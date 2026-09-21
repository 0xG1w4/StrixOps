"""Startup probes use only synthetic prompts and in-memory HTTP transports."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace

import httpx
import pytest

from strixops.config.context import ContextSettings
from strixops.config.settings import EngineSettings
from strixops.engine import context_probe
from strixops.engine.model_capacity import ModelCapacity


@pytest.fixture
def settings():
    return EngineSettings(
        llm_api_base="https://provider.invalid/v1", llm_api_key="private-probe-key",
        strix_llm="litellm/openrouter/vendor/model", strix_runs="", operator_hints_dir="",
        host_workspace_dir="", dry_run=False, llm_api_mode="responses", llm_reasoning_effort="high",
    )


@pytest.fixture
def context():
    return ContextSettings()


@pytest.fixture
def capacity():
    return ModelCapacity(
        "vendor/model", 200_000, 8_192, "configured_fallback", "configured_fallback", "no_metadata",
    )


@pytest.fixture
def transport(monkeypatch):
    original = httpx.AsyncClient
    requests, clients, options = [], [], []

    def install(handler):
        async def respond(request):
            requests.append(request)
            response = handler(request)
            return await response if hasattr(response, "__await__") else response

        def factory(**kwargs):
            options.append(kwargs)
            client = original(**kwargs, transport=httpx.MockTransport(respond))
            clients.append(client)
            return client

        monkeypatch.setattr(context_probe.httpx, "AsyncClient", factory)
        return requests, clients, options

    return install


def _body(request):
    return json.loads(request.content)


def _text(request):
    body = _body(request)
    return (body.get("input") or body["messages"])[0]["content"]


def _success(request, *, count=None, receipt=True):
    text = _text(request)
    first = re.search(r"First marker: (\w+)", text).group(1)
    last = re.search(r"Last marker: (\w+)", text).group(1)
    output = f"{first} {last}" if receipt else "ignored markers"
    count = count if count is not None else len(text) // 2
    if request.url.path.endswith("/responses"):
        return {
            "status": "completed", "truncation": "disabled",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": output}]}],
            "usage": {"input_tokens": count, "output_tokens": 8, "total_tokens": count + 8},
        }
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": output}}],
        "usage": {"prompt_tokens": count, "completion_tokens": 8, "total_tokens": count + 8},
    }


def _ok(request):
    return httpx.Response(200, json=_success(request))


async def test_metadata_skips_inference(settings, context, capacity, transport):
    seen, clients, _ = transport(_ok)
    result = await context_probe.probe_model_capacity(
        settings, context, replace(capacity, capacity_source="provider_metadata"),
    )
    assert result.probe.status == "skipped_metadata"
    assert result.probe.requests == 0 and not seen and not clients


async def test_disabled_probe_is_free(settings, capacity, transport, monkeypatch):
    monkeypatch.setenv("STRIX_CONTEXT_PROBE_ENABLED", "false")
    seen, clients, _ = transport(_ok)
    result = await context_probe.probe_model_capacity(settings, ContextSettings(), capacity)
    assert result.probe.status == "disabled" and result.probe.requests == 0
    assert not seen and not clients


async def test_successes_are_lower_bounds_with_bounded_spend(settings, context, capacity, transport):
    seen, clients, options = transport(_ok)
    result = await context_probe.probe_model_capacity(settings, context, capacity)
    assert result.capacity_tokens == capacity.capacity_tokens
    assert result.capacity_source == "configured_fallback"
    assert result.probe.status == "completed"
    assert result.probe.requests == 3
    assert result.probe.planned_input_tokens == 8_192 + 16_384 + 32_768
    assert result.probe.largest_accepted_input_tokens == (32_768 - 256) // 2
    assert result.probe.smallest_rejected_input_tokens is None
    assert all(client.is_closed for client in clients)
    assert options == [{"trust_env": False, "follow_redirects": False}]
    for request, target in zip(seen, (8_192, 16_384, 32_768), strict=True):
        body = _body(request)
        assert str(request.url) == "https://provider.invalid/v1/responses"
        assert request.headers["authorization"] == "Bearer private-probe-key"
        assert request.headers["accept-encoding"] == "identity"
        assert len(_text(request).encode()) + 256 == target
        assert body["model"] == "vendor/model" and body["reasoning"] == {"effort": "high"}
        assert body["max_output_tokens"] == 64 and body["truncation"] == "disabled"
        assert body["store"] is False and body["stream"] is False and "tools" not in body
        assert "private-probe-key" not in request.content.decode()


async def test_probe_snapshot_is_idempotent_but_new_run_is_not_cached(settings, context, capacity, transport):
    seen, _, _ = transport(_ok)
    first = await context_probe.probe_model_capacity(settings, context, capacity)
    assert await context_probe.probe_model_capacity(settings, context, first) is first
    assert len(seen) == 3
    second = await context_probe.probe_model_capacity(
        replace(settings, llm_api_base="https://another.invalid/v1"), context, capacity,
    )
    assert second.probe.requests == 3 and len(seen) == 6
    assert seen[-1].url.host == "another.invalid"


@pytest.mark.parametrize("budget,requests,spent", [
    (8_192, 1, 8_192), (24_576, 2, 24_576), (16_000, 1, 8_192),
])
async def test_total_budget_stops_before_overspending(settings, capacity, transport, budget, requests, spent):
    seen, _, _ = transport(_ok)
    result = await context_probe.probe_model_capacity(
        settings, ContextSettings(probe_total_input_tokens=budget), capacity,
    )
    assert len(seen) == result.probe.requests == requests
    assert result.probe.planned_input_tokens == spent <= budget


async def test_request_limit_and_known_output_limit(settings, capacity, transport):
    seen, _, _ = transport(_ok)
    result = await context_probe.probe_model_capacity(
        settings, ContextSettings(probe_max_requests=1), replace(capacity, output_limit_tokens=32),
    )
    assert len(seen) == 1 and result.probe.status == "budget_exhausted"
    assert _body(seen[0])["max_output_tokens"] == result.probe.output_budget_tokens == 32


async def test_generic_chat_success_is_unverified_because_truncation_cannot_be_disabled(
    settings, context, capacity, transport,
):
    seen, _, _ = transport(_ok)
    result = await context_probe.probe_model_capacity(
        replace(settings, llm_api_mode="chat_completions"), context, capacity,
    )
    assert result.probe.status == "unverified" and result.probe.largest_accepted_input_tokens is None
    assert len(seen) == 1
    body = _body(seen[0])
    assert body["max_tokens"] == 64 and body["reasoning_effort"] == "high"
    assert "plugins" not in body and "truncation" not in body


async def test_openrouter_chat_disables_context_compression(settings, context, capacity, transport):
    seen, _, _ = transport(_ok)
    result = await context_probe.probe_model_capacity(
        replace(settings, llm_api_mode="chat_completions", llm_api_base="https://openrouter.ai/api/v1"),
        context, capacity,
    )
    assert result.probe.status == "completed" and result.probe.largest_accepted_input_tokens
    assert _body(seen[0])["plugins"] == [{"id": "context-compression", "enabled": False}]


@pytest.mark.parametrize("base", ["https://openrouter.ai.evil.invalid/v1", "http://openrouter.ai/api/v1",
                                 "https://openrouter.ai:444/api/v1"])
async def test_openrouter_parameters_do_not_leak_to_other_routes(
    settings, context, capacity, transport, base,
):
    seen, _, _ = transport(_ok)
    await context_probe.probe_model_capacity(
        replace(settings, llm_api_mode="chat_completions", llm_api_base=base), context, capacity,
    )
    assert "plugins" not in _body(seen[0])


async def test_default_reasoning_is_omitted(settings, context, capacity, transport):
    seen, _, _ = transport(_ok)
    await context_probe.probe_model_capacity(
        replace(settings, llm_reasoning_effort="default"), context, capacity,
    )
    assert "reasoning" not in _body(seen[0])


@pytest.mark.parametrize("error", [
    {"code": "context_length_exceeded", "message": "Maximum context length is 4,096 tokens."},
    {"code": "context_length_exceeded", "max_context_length": 4_096},
    {"message": "This model's maximum context length is 4096 tokens. However, you requested 8000 tokens."},
    {"message": "prompt is too long: 8000 tokens > 4096 maximum"},
])
async def test_only_explicit_provider_limit_changes_capacity(settings, context, capacity, transport, error):
    seen, _, _ = transport(lambda request: httpx.Response(400, json={"error": error}))
    result = await context_probe.probe_model_capacity(settings, context, capacity)
    assert result.capacity_tokens == 4_096 and result.capacity_source == "provider_error"
    assert result.probe.status == "limit_reported" and len(seen) == 1


@pytest.mark.parametrize("status", [400, 413, 429, 500, 302])
async def test_generic_http_failures_are_not_context_limits(settings, context, capacity, transport, status):
    seen, _, _ = transport(lambda request: httpx.Response(status, json={
        "error": {"message": "private-probe-key https://secret.invalid/path", "max_tokens": 4_096},
    }, headers={"location": "https://redirect.invalid"}))
    result = await context_probe.probe_model_capacity(settings, context, capacity)
    assert result.capacity_tokens == capacity.capacity_tokens and result.probe.status == "failed"
    assert len(seen) == 1
    public = json.dumps(result.to_dict())
    for secret in ("private-probe-key", "secret.invalid", "redirect.invalid", "provider.invalid"):
        assert secret not in public


async def test_http_413_without_explicit_limit_does_not_binary_search(settings, context, capacity, transport):
    seen, _, _ = transport(lambda request: httpx.Response(413, json={
        "error": {"code": "context_length_exceeded", "message": "Too large"},
    }))
    result = await context_probe.probe_model_capacity(settings, context, capacity)
    assert result.probe.status == "failed" and len(seen) == 1
    assert result.capacity_source == "configured_fallback"


@pytest.mark.parametrize("invalid", [True, -1, 0, 1.5, "4096", 100_000_001])
async def test_invalid_error_limit_is_not_adopted(settings, capacity, transport, invalid):
    transport(lambda request: httpx.Response(400, json={
        "error": {"code": "context_length_exceeded", "max_context_length": invalid, "max_tokens": 100},
    }))
    result = await context_probe.probe_model_capacity(
        settings, ContextSettings(probe_max_requests=1), capacity,
    )
    assert result.capacity_tokens == capacity.capacity_tokens


async def test_context_error_without_limit_can_bisect_but_never_claim_maximum(settings, capacity, transport):
    def respond(request):
        if len(_text(request)) > 6_000:
            return httpx.Response(400, json={"error": {"code": "context_length_exceeded"}})
        return _ok(request)

    seen, _, _ = transport(respond)
    result = await context_probe.probe_model_capacity(
        settings, ContextSettings(probe_max_requests=4), capacity,
    )
    assert [len(_text(request)) + 256 for request in seen] == [8_192, 4_096, 6_144, 7_168]
    assert result.capacity_source == "configured_fallback" and result.capacity_tokens == 200_000
    assert result.probe.smallest_rejected_input_tokens is None


async def test_rejection_count_requires_provider_input_measurement(settings, context, capacity, transport):
    transport(lambda request: httpx.Response(400, json={"error": {
        "code": "context_length_exceeded", "max_context_length": 4_096, "input_tokens": 7_000,
    }}))
    result = await context_probe.probe_model_capacity(settings, context, capacity)
    assert result.probe.smallest_rejected_input_tokens == 7_000


@pytest.mark.parametrize("limit", [1_000, 50_000])
async def test_contradictory_error_limit_does_not_replace_capacity(
    settings, context, capacity, transport, limit,
):
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _ok(request)
        return httpx.Response(400, json={"error": {
            "code": "context_length_exceeded", "max_context_length": limit,
        }})

    transport(respond)
    result = await context_probe.probe_model_capacity(settings, context, capacity)
    assert result.capacity_tokens == capacity.capacity_tokens
    assert result.probe.status == "unverified"


@pytest.mark.parametrize("change", [
    {"usage": {}}, {"usage": {"input_tokens": True}}, {"usage": {"input_tokens": 0}},
    {"usage": {"input_tokens": 100_000}}, {"usage": {"total_tokens": 1000}},
    {"truncation": "auto"}, {"truncation": {}}, {"truncation": []}, {"input_truncated": True},
    {"status": "incomplete"}, {"output": None}, {"output": "invalid"},
    {"output": [{"type": "message", "content": None}]},
    {"output": [{"type": "message", "content": {}}]},
    {"output": [{"type": "message", "content": "text"}]},
    {"output": [{"type": "message", "content": [{"type": "output_text", "text": {}}]}]},
])
async def test_missing_usage_truncation_or_malformed_output_is_unverified(
    settings, context, capacity, transport, change,
):
    seen, _, _ = transport(lambda request: httpx.Response(200, json={**_success(request), **change}))
    result = await context_probe.probe_model_capacity(settings, context, capacity)
    assert result.probe.status == "unverified" and result.probe.largest_accepted_input_tokens is None
    assert result.capacity_tokens == capacity.capacity_tokens and len(seen) == 1


async def test_marker_failure_does_not_claim_capacity(settings, context, capacity, transport):
    transport(lambda request: httpx.Response(200, json=_success(request, receipt=False)))
    result = await context_probe.probe_model_capacity(settings, context, capacity)
    assert result.probe.status == "unverified" and result.probe.largest_accepted_input_tokens is None


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
async def test_paid_usage_reported_once_even_when_receipt_fails(settings, capacity, transport, mode):
    transport(lambda request: httpx.Response(200, json=_success(request, count=2_000, receipt=False)))
    totals = []
    result = await context_probe.probe_model_capacity(
        replace(settings, llm_api_mode=mode), ContextSettings(probe_max_requests=1), capacity,
        on_usage=totals.append,
    )
    assert result.probe.status == "unverified"
    assert totals == [{"input_tokens": 2_000, "output_tokens": 8, "total_tokens": 2_008}]
    assert "usage" not in result.to_dict()["probe"]


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 20},
                                   {"input_tokens": True, "output_tokens": 1, "total_tokens": 2},
                                   {"input_tokens": 20, "output_tokens": 1, "total_tokens": 2}])
async def test_incomplete_or_invalid_usage_does_not_invent_accounting(settings, capacity, transport, usage):
    transport(lambda request: httpx.Response(200, json={**_success(request), "usage": usage}))
    totals = []
    await context_probe.probe_model_capacity(
        settings, ContextSettings(probe_max_requests=1), capacity, on_usage=totals.append,
    )
    assert totals == []


async def test_accounting_callback_failure_does_not_break_probe(settings, capacity, transport):
    transport(_ok)

    def broken(totals):
        raise RuntimeError("sink unavailable")

    result = await context_probe.probe_model_capacity(
        settings, ContextSettings(probe_max_requests=1), capacity, on_usage=broken,
    )
    assert result.probe.largest_accepted_input_tokens is not None


class EndlessStream(httpx.AsyncByteStream):
    def __init__(self, *, large=False):
        self.large = large
        self.closed = False

    async def __aiter__(self):
        while True:
            yield b" " * (16 * 1024 if self.large else 1)
            await asyncio.sleep(0 if self.large else 0.005)

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("timeouts", [(0.025, 1), (1, 0.025)])
async def test_absolute_and_individual_timeout_close_body(settings, capacity, transport, timeouts):
    stream = EndlessStream()
    seen, clients, _ = transport(lambda request: httpx.Response(200, stream=stream))
    context = ContextSettings(probe_timeout_seconds=timeouts[0], probe_request_timeout_seconds=timeouts[1])
    async with asyncio.timeout(1):
        result = await context_probe.probe_model_capacity(settings, context, capacity)
    assert result.probe.status == "timeout" and len(seen) == 1
    assert stream.closed and all(client.is_closed for client in clients)


async def test_body_size_is_bounded(settings, context, capacity, transport):
    stream = EndlessStream(large=True)
    transport(lambda request: httpx.Response(200, stream=stream))
    result = await context_probe.probe_model_capacity(settings, context, capacity)
    assert result.probe.status == "failed" and stream.closed


@pytest.mark.parametrize("headers", [{"content-encoding": "gzip"}, {"content-length": "999999999"}])
async def test_unsupported_body_is_rejected_before_reading(settings, context, capacity, transport, headers):
    stream = EndlessStream()
    transport(lambda request: httpx.Response(200, stream=stream, headers=headers))
    result = await context_probe.probe_model_capacity(settings, context, capacity)
    assert result.probe.status == "failed" and stream.closed


async def test_cancellation_is_propagated_and_client_closed(settings, context, capacity, transport):
    def cancel(request):
        raise asyncio.CancelledError

    _, clients, _ = transport(cancel)
    with pytest.raises(asyncio.CancelledError):
        await context_probe.probe_model_capacity(settings, context, capacity)
    assert all(client.is_closed for client in clients)


async def test_incompatible_snapshot_never_sends_credentials(settings, context, capacity, transport):
    seen, _, _ = transport(_ok)
    result = await context_probe.probe_model_capacity(
        settings, context, replace(capacity, model="other-model"),
    )
    assert result.probe.status == "failed" and not seen
