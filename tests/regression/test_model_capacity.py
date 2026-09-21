"""Bounded, credential-safe metadata lookup against in-memory HTTP transports."""

from __future__ import annotations

import asyncio
import builtins
import json
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import httpx
import pytest

from strixops.config.context import ContextSettings
from strixops.config.settings import EngineSettings
from strixops.engine import model_capacity


@pytest.fixture
def settings():
    return EngineSettings(
        llm_api_base="https://provider.invalid/v1",
        llm_api_key="private-test-key",
        strix_llm="gateway-model",
        strix_runs="", operator_hints_dir="", host_workspace_dir="", dry_run=False,
    )


@pytest.fixture
def context():
    return ContextSettings(fallback_context_tokens=31_000)


@pytest.fixture(autouse=True)
def catalog(tmp_path, monkeypatch):
    path = tmp_path / "model_prices_and_context_window_backup.json"
    path.write_text("{}")
    monkeypatch.setattr(
        model_capacity, "distribution", lambda name: SimpleNamespace(locate_file=lambda relative: path),
    )
    return path


async def resolve(settings, context, response):
    def handler(request):
        return response(request) if callable(response) else response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await model_capacity.resolve_model_capacity(settings, context, http_client=client)
        assert not client.is_closed
        return result


def payload(*items):
    return httpx.Response(200, json={"data": list(items)})


async def test_exact_wire_model_and_conservative_metadata(settings, context):
    settings = replace(settings, strix_llm="litellm/openrouter/openai/example")

    def respond(request):
        assert request.method == "GET"
        assert str(request.url) == "https://provider.invalid/v1/models"
        assert request.headers["authorization"] == "Bearer private-test-key"
        assert request.headers["accept-encoding"] == "identity"
        return payload(
            {"id": "example", "context_length": 1},
            {
                "id": "openai/example", "context_length": 128_000, "context_window": 96_000,
                "max_input_tokens": 80_000, "max_model_len": 70_000, "max_tokens": 1,
                "max_output_tokens": 10_000,
                "top_provider": {"context_length": 64_000, "max_completion_tokens": 9_000},
            },
        )

    result = await resolve(settings, context, respond)
    assert result.to_dict() == {
        "model": "openai/example", "capacity_tokens": 64_000, "output_limit_tokens": 9_000,
        "capacity_source": "provider_metadata", "output_source": "provider_metadata",
        "lookup_status": "resolved",
    }
    with pytest.raises(FrozenInstanceError):
        result.capacity_tokens = 10


@pytest.mark.parametrize("field", ["context_length", "context_window", "max_input_tokens", "max_model_len"])
async def test_explicit_capacity_fields(settings, context, field):
    result = await resolve(settings, context, payload({"id": "gateway-model", field: 12_000}))
    assert result.capacity_tokens == 12_000
    assert result.capacity_source == "provider_metadata"


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.25, "64000", None, 100_000_001, 10**100])
async def test_invalid_limits_do_not_become_capacity_or_output(settings, context, value):
    result = await resolve(settings, context, payload({
        "id": "gateway-model", "context_length": value, "max_output_tokens": value,
        "max_tokens": 400_000, "top_provider": {"context_length": value, "max_completion_tokens": value},
    }))
    assert result.capacity_tokens == 31_000
    assert result.output_limit_tokens == 8_192
    assert result.capacity_source == result.output_source == "configured_fallback"
    assert result.lookup_status == "no_metadata"


async def test_duplicate_exact_ids_take_smaller_published_limits(settings, context):
    result = await resolve(settings, context, payload(
        {"id": "gateway-model", "context_length": 128_000, "max_output_tokens": 16_000},
        {"id": "gateway-model", "context_window": 64_000, "max_output_tokens": 8_000},
    ))
    assert (result.capacity_tokens, result.output_limit_tokens) == (64_000, 8_000)


async def test_output_metadata_does_not_imply_context(settings, context):
    result = await resolve(settings, context, payload({"id": "gateway-model", "max_output_tokens": 16_000}))
    assert result.capacity_tokens == 31_000
    assert result.capacity_source == "configured_fallback"
    assert result.output_limit_tokens == 16_000
    assert result.output_source == "provider_metadata"
    assert result.lookup_status == "no_metadata"


async def test_offline_catalog_fills_only_missing_limits(settings, context, catalog):
    catalog.write_text(json.dumps({
        "gateway-model": {"max_input_tokens": 200_000, "max_output_tokens": 12_000},
    }))
    result = await resolve(settings, context, payload({"id": "gateway-model", "context_length": 32_000}))
    assert result.capacity_tokens == 32_000
    assert result.capacity_source == "provider_metadata"
    assert result.output_limit_tokens == 12_000
    assert result.output_source == "model_catalog"


async def test_catalog_fallback_does_not_import_litellm(settings, context, catalog, monkeypatch):
    catalog.write_text(json.dumps({
        "gateway-model": {"max_input_tokens": 90_000, "max_output_tokens": 4_000},
    }))
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name != "litellm" and not name.startswith("litellm.")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = await resolve(settings, context, payload({"id": "gateway-model"}))
    assert result.capacity_tokens == 90_000
    assert result.output_limit_tokens == 4_000
    assert result.capacity_source == result.output_source == "model_catalog"


async def test_catalog_routing_wrapper_and_no_max_tokens_context(settings, context, catalog):
    settings = replace(settings, strix_llm="openrouter/vendor/model")
    catalog.write_text(json.dumps({
        "openrouter/vendor/model": {"max_tokens": 200_000, "max_output_tokens": 2_000},
    }))
    result = await resolve(settings, context, payload({"id": "vendor/model"}))
    assert result.capacity_tokens == 31_000
    assert result.output_limit_tokens == 2_000


@pytest.mark.parametrize("body", ["[]", "{", '{"gateway-model": {"max_tokens": 90000}}'])
async def test_bad_or_ambiguous_catalog_uses_configured_fallback(settings, context, catalog, body):
    catalog.write_text(body)
    result = await resolve(settings, context, payload({"id": "gateway-model"}))
    assert result.capacity_tokens == 31_000
    assert result.capacity_source == "configured_fallback"


async def test_missing_catalog_uses_configured_fallback(settings, context, catalog):
    catalog.unlink()
    result = await resolve(settings, context, payload({"id": "gateway-model"}))
    assert result.capacity_tokens == 31_000


@pytest.mark.parametrize("status, expected", [(302, "redirect"), (401, "auth_error"), (403, "auth_error"),
                                            (404, "http_error"), (500, "http_error")])
async def test_http_failure_and_redirect_are_safe(settings, context, status, expected):
    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(status, headers={"location": "https://other.invalid/private-test-key"},
                              text="private-test-key upstream secret details")

    # Override even a supplied client's redirect policy.
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        result = await model_capacity.resolve_model_capacity(settings, context, http_client=client)
    assert seen == ["https://provider.invalid/v1/models"]
    assert result.lookup_status == expected
    serialized = json.dumps(result.to_dict())
    assert "private-test-key" not in serialized and "provider.invalid" not in serialized
    assert "other.invalid" not in serialized and "upstream secret" not in serialized


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"data":{}}', b'{"data":null}', b"\xff"])
async def test_invalid_catalog_response(settings, context, body):
    result = await resolve(settings, context, httpx.Response(200, content=body))
    assert result.lookup_status == "invalid_response"
    assert result.capacity_source == "configured_fallback"


@pytest.mark.parametrize("items", [[], [{"id": "GATEWAY-MODEL", "context_length": 1}],
                                      [{"id": "vendor/gateway-model", "context_length": 1}]])
async def test_no_fuzzy_model_match(settings, context, items):
    result = await resolve(settings, context, payload(*items))
    assert result.lookup_status == "model_not_found"
    assert result.capacity_tokens == 31_000


@pytest.mark.parametrize("base", ["ftp://host/v1", "https://user:private-test-key@host/v1", "https://host/v1?key=x",
                                     "https://host/v1#x", "https://host:99999/v1", "https://host\\evil/v1"])
async def test_invalid_endpoints_never_receive_credentials(settings, context, base):
    def unexpected_request(request):
        pytest.fail("invalid endpoint must be rejected before HTTP")

    result = await resolve(replace(settings, llm_api_base=base), context, unexpected_request)
    assert result.lookup_status == "invalid_endpoint"


@pytest.mark.parametrize("error, status", [
    (httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "connection_error"),
])
async def test_transport_error_is_sanitized(settings, context, error, status):
    def respond(request):
        raise error("https://provider.invalid/private-test-key upstream detail", request=request)

    result = await resolve(settings, context, respond)
    assert result.lookup_status == status
    assert "private-test-key" not in repr(result)
    assert "provider.invalid" not in repr(result)


async def test_caller_cancellation_is_not_converted_to_fallback(settings, context):
    def respond(request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await resolve(settings, context, respond)


async def test_caller_client_auth_cannot_replace_route_credentials(settings, context):
    def respond(request):
        assert request.headers["authorization"] == "Bearer private-test-key"
        return payload({"id": "gateway-model", "context_length": 12_000})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), auth=("ambient-user", "ambient-password"),
    ) as client:
        result = await model_capacity.resolve_model_capacity(settings, context, http_client=client)
    assert result.capacity_tokens == 12_000


class SlowStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        while True:
            yield b" "
            await asyncio.sleep(0.005)


async def test_absolute_timeout_applies_to_dripping_response(settings, context, monkeypatch):
    monkeypatch.setattr(model_capacity, "REQUEST_TIMEOUT_SECONDS", 0.03)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=SlowStream()),
    )) as client:
        async with asyncio.timeout(1):
            result = await model_capacity.resolve_model_capacity(settings, context, http_client=client)
    assert result.lookup_status == "timeout"


class LargeStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        for _ in range(3):
            yield b" " * 64 * 1024

    async def aclose(self):
        self.closed = True


async def test_streamed_body_limit_closes_response(settings, context, monkeypatch):
    monkeypatch.setattr(model_capacity, "MAX_RESPONSE_BYTES", 64 * 1024)
    stream = LargeStream()
    result = await resolve(settings, context, httpx.Response(200, stream=stream))
    assert result.lookup_status == "response_too_large"
    assert stream.closed


async def test_declared_body_limit_is_rejected_without_reading(settings, context):
    result = await resolve(settings, context, httpx.Response(
        200, headers={"content-length": str(model_capacity.MAX_RESPONSE_BYTES + 1)}, stream=SlowStream(),
    ))
    assert result.lookup_status == "response_too_large"


async def test_unrequested_encoding_is_rejected_before_decompression(settings, context):
    result = await resolve(settings, context, httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=SlowStream(),
    ))
    assert result.lookup_status == "invalid_response"


async def test_same_name_on_two_endpoints_is_not_cached(settings, context):
    seen = []

    def respond(request):
        seen.append(request.url.host)
        capacity = 64_000 if request.url.host == "provider.invalid" else 16_000
        return payload({"id": "gateway-model", "context_length": capacity})

    first = await resolve(settings, context, respond)
    second = await resolve(replace(settings, llm_api_base="https://other.invalid/v1"), context, respond)
    assert (first.capacity_tokens, second.capacity_tokens) == (64_000, 16_000)
    assert seen == ["provider.invalid", "other.invalid"]


async def test_owned_client_disables_ambient_routing(settings, context, monkeypatch):
    original_client = httpx.AsyncClient
    options = []

    def client_factory(**kwargs):
        options.append(kwargs)
        return original_client(**kwargs, transport=httpx.MockTransport(
            lambda request: payload({"id": "gateway-model", "context_length": 16_000}),
        ))

    monkeypatch.setattr(model_capacity.httpx, "AsyncClient", client_factory)
    result = await model_capacity.resolve_model_capacity(settings, context)
    assert result.capacity_tokens == 16_000
    assert options == [{"trust_env": False, "follow_redirects": False}]
