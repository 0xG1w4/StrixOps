"""Responses SSE errors through the real HTTP parser and agent retry runner."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from agents import Agent, RunConfig, Runner, SQLiteSession
from agents.retry import ModelRetryBackoffSettings
from agents.sandbox.runtime_session_manager import SandboxRuntimeSessionManager
from openai import APIError

from strixops.agents.factory import build_root_agent
from strixops.config.model_errors import model_error_details
from strixops.config.provider import make_platform_model
from strixops.engine.loop import run_config
from strixops.engine.resilience import model_settings
from strixops.engine.scanconfig import EngineContext, ScanSpec
from strixops.engine.usage import UsageAccumulator, UsageTrackingModel
from tests.unit.test_filesystem_tools import MemorySandbox
from tests.unit.test_model_errors import azure_policy_message
from tests.unit.test_model_provider import _message, _response, _response_stream, _settings


def _failed_stream(error, *, progress="none"):
    events = []
    if progress != "none":
        response = {**_response([]), "status": "in_progress"}
        events.extend(
            [
                {"type": "response.created", "response": response},
                {"type": "response.in_progress", "response": response},
            ]
        )
    if progress == "text":
        events.append(
            {
                "type": "response.output_text.delta",
                "item_id": "msg_fixture",
                "output_index": 0,
                "content_index": 0,
                "delta": "Partial output",
                "logprobs": [],
            }
        )
    if progress == "tool":
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "fc_partial",
                    "call_id": "call_partial",
                    "name": "some_tool",
                    "arguments": "",
                    "status": "in_progress",
                },
            }
        )
    if progress == "reasoning":
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "reasoning", "id": "rs_partial", "summary": []},
            }
        )
    events.append({"error": {"message": "litellm.APIError: Response API in-stream error", **error}})
    body = "".join(
        f"data: {json.dumps({**event, 'sequence_number': index})}\n\n"
        for index, event in enumerate(events)
    )
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


async def _run(handler, **kwargs):
    settings = model_settings()
    settings.retry = replace(settings.retry, backoff=ModelRetryBackoffSettings(initial_delay=0))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        agent = Agent(
            name="Retry fixture",
            model=make_platform_model(_settings(mode="responses"), http_client=client),
        )
        result = Runner.run_streamed(
            agent,
            input="Reply with Done.",
            run_config=RunConfig(tracing_disabled=True, model_settings=settings),
            **kwargs,
        )
        async for _ in result.stream_events():
            pass
        return result.final_output


@pytest.mark.parametrize("progress", ["none", "metadata"])
@pytest.mark.parametrize(
    "error",
    [
        {"code": 500},
        {"code": "503"},
        {"status_code": 429},
        {"status": 504, "code": None},
        {"code": "server_error"},
        {"code": "rate_limit_exceeded"},
        {"type": "internal_server_error"},
    ],
)
async def test_transient_sse_error_retries_before_meaningful_events(error, progress):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return _failed_stream(error, progress=progress)
        return _response_stream(_response([_message("Done")]))

    assert await _run(respond) == "Done"
    assert len(requests) == 2
    assert requests[0] == requests[1]


@pytest.mark.parametrize(
    "error",
    [
        {},
        {"type": "api_error"},
        {"code": "unknown_error"},
        {"code": 400},
        {"status_code": "401"},
        {"status": 403},
        {"code": "invalid_request_error", "status_code": 500},
        {"type": "authentication_error", "code": 500},
        {"type": "bad_request_error", "code": 500},
        {"type": "unauthorized", "code": 500},
        {"type": "forbidden", "code": 500},
        {"code": "context_length_exceeded", "status_code": 500},
        {"code": "insufficient_quota", "status_code": 429},
        {"code": "server_error", "status_code": 400},
        {"code": 500, "error": {"code": "invalid_request_error"}},
        {"code": 500, "innererror": {"type": "authentication_error"}},
        {"code": 500, "error": {"innererror": {"status_code": 403}}},
        {"code": "cyber_policy"},
        {"code": 500, "message": azure_policy_message()},
        {"code": 500, "error": json.dumps({"error": {"code": "cyber_policy"}})},
    ],
)
async def test_unknown_or_permanent_sse_error_is_not_retried(error):
    requests = []

    def respond(request):
        requests.append(request)
        return _failed_stream(error)

    with pytest.raises(APIError) as caught:
        await _run(respond)
    assert len(requests) == 1
    assert "model_request_attempt=1" in model_error_details(caught.value)


@pytest.mark.parametrize("progress", ["text", "tool", "reasoning"])
async def test_transient_sse_error_never_replays_after_output_or_tool_event(progress):
    requests = []

    def respond(request):
        requests.append(request)
        return _failed_stream({"code": 500}, progress=progress)

    with pytest.raises(APIError) as caught:
        await _run(respond)
    assert len(requests) == 1
    details = model_error_details(caught.value)
    assert "model_request_attempt=1" in details
    assert "stream_events=3" in details
    blocked_type = "response.output_text.delta" if progress == "text" else "response.output_item.added"
    assert f"retry_blocked_by={blocked_type}" in details


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_http_bad_request_and_auth_errors_are_not_retried(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "Rejected", "code": 500}})

    with pytest.raises(APIError):
        await _run(respond)
    assert len(requests) == 1


async def test_transient_sse_error_keeps_existing_retry_limit():
    requests = []

    def respond(request):
        requests.append(request)
        return _failed_stream({"code": 500})

    with pytest.raises(APIError) as caught:
        await _run(respond)
    assert len(requests) == 6  # Original attempt plus the existing five retries.
    assert "model_request_attempt=6" in model_error_details(caught.value)
    assert "stream_events=0" in model_error_details(caught.value)


async def test_transient_sse_error_does_not_override_stateful_replay_guard():
    requests = []

    def respond(request):
        requests.append(request)
        return _failed_stream({"code": 500})

    with pytest.raises(APIError):
        await _run(respond, previous_response_id="resp_previous")
    assert len(requests) == 1


async def test_live_sandbox_root_keeps_retry_policy_through_preparation_and_usage_wrapper(monkeypatch):
    # Exercise real SandboxRuntime preparation, the complete root tool list,
    # persisted chat session, engine RunConfig and usage delegation. Only the
    # container session provider and model HTTP transport are replaced.
    sandbox = MemorySandbox()

    async def ensure_session(self, **kwargs):
        return sandbox

    monkeypatch.setattr(SandboxRuntimeSessionManager, "ensure_session", ensure_session)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return _failed_stream({"code": 500}, progress="metadata")
        return _response_stream(_response([_message("Done")]))

    config = run_config(SimpleNamespace(client=None, session=sandbox))
    config.model_settings.retry = replace(
        config.model_settings.retry, backoff=ModelRetryBackoffSettings(initial_delay=0)
    )
    session = SQLiteSession("sandbox-retry-fixture")
    accumulator = UsageAccumulator()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            model = UsageTrackingModel(
                make_platform_model(_settings(mode="responses"), http_client=client), accumulator
            )
            agent = build_root_agent(ScanSpec(target="https://scan.invalid"), model=model, sandbox=True)
            result = Runner.run_streamed(
                agent,
                input="Reply with Done.",
                context=EngineContext(agent_id="root", agent_name="root"),
                session=session,
                run_config=config,
            )
            async for _ in result.stream_events():
                pass
    finally:
        session.close()
    assert result.final_output == "Done"
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert {"finish_scan", "exec_command", "write_stdin", "apply_patch"} <= {
        tool["name"] for tool in requests[0]["tools"]
    }
    assert accumulator.snapshot()["requests"] == 1
