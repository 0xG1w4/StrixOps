"""Preserve Responses SSE diagnostics without changing the SDK error or cleanup."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from agents import ModelSettings, ModelTracing
from openai import APIError, AsyncOpenAI

from strixops.config.model_errors import model_error_details
from strixops.config.responses_transport import PlatformResponsesModel


def _response(status="completed"):
    return {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 0,
        "model": "fixture-model",
        "status": status,
        "output": []
        if status == "in_progress"
        else [
            {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "OK", "annotations": []}],
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }


def _sse(event):
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, events, *, block=False):
        self.events = events
        self.block = block
        self.waiting = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0

    async def __aiter__(self):
        for event in self.events:
            yield _sse(event)
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.close_calls += 1
        self.closed.set()


def _kwargs():
    return {
        "system_instructions": None,
        "input": "Fixture request",
        "model_settings": ModelSettings(),
        "tools": [],
        "output_schema": None,
        "handoffs": [],
        "previous_response_id": None,
        "conversation_id": None,
    }


def _client(stream, *, headers=None):
    def respond(request):
        return httpx.Response(
            200,
            stream=stream,
            headers={
                "content-type": "text/event-stream",
                **({"x-request-id": "req_fixture"} if headers is None else headers),
            },
        )

    return AsyncOpenAI(
        api_key="fixture-key",
        base_url="https://gateway.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )


@pytest.mark.parametrize("after_output", [False, True])
async def test_in_stream_error_retains_original_exception_body_and_http_request_id(monkeypatch, after_output):
    error = {
        "message": "litellm.APIError: Response API in-stream error",
        "code": "upstream_failure",
        "type": "server_error",
        "status_code": 503,
        "details": {"provider": "fixture"},
    }
    events = [{"type": "response.created", "response": _response("in_progress"), "sequence_number": 0}]
    if after_output:
        events.append(
            {
                "type": "response.output_text.delta",
                "item_id": "msg_fixture",
                "output_index": 0,
                "content_index": 0,
                "delta": "partial output",
                "logprobs": [],
                "sequence_number": 1,
            }
        )
    events.append({"type": "error", "error": error})
    stream = TrackedStream(events)
    created_errors = []

    def record_error(*args, **kwargs):
        exc = APIError(*args, **kwargs)
        created_errors.append(exc)
        return exc

    monkeypatch.setattr("openai._streaming.APIError", record_error)
    seen = []
    async with _client(stream) as client:
        model = PlatformResponsesModel(model="fixture-model", openai_client=client)
        with pytest.raises(APIError) as caught:
            async for event in model.stream_response(**_kwargs(), tracing=ModelTracing.DISABLED):
                seen.append(event.type)

        assert caught.value is created_errors[0]
        assert type(caught.value) is APIError
        assert caught.value.body == error
        assert caught.value.request_id == "req_fixture"
        assert str(caught.value) == error["message"]
        assert caught.value._strixops_stream_event_count == (2 if after_output else 1)
        assert getattr(caught.value, "_strixops_stream_retry_blocked_by", None) == (
            "response.output_text.delta" if after_output else None
        )
        assert ("response.output_text.delta" in seen) is after_output
        assert stream.closed.is_set()
        assert stream.close_calls == 1


@pytest.mark.parametrize("with_request_id", [False, True])
async def test_gateway_request_id_remains_distinct_from_provider_request_id(with_request_id):
    headers = {"x-litellm-call-id": "call-gateway-fixture"}
    if with_request_id:
        headers["x-request-id"] = "req-provider-fixture"
    stream = TrackedStream([{"type": "error", "error": {"message": "failed", "code": 500}}])
    async with _client(stream, headers=headers) as client:
        model = PlatformResponsesModel(model="fixture-model", openai_client=client)
        with pytest.raises(APIError) as caught:
            async for _ in model.stream_response(**_kwargs(), tracing=ModelTracing.DISABLED):
                pass
    details = model_error_details(caught.value)
    assert "gateway_request_id=call-gateway-fixture" in details
    assert getattr(caught.value, "request_id", None) == (
        "req-provider-fixture" if with_request_id else None
    )
    assert "stream_events=0" in details
    assert "retry_blocked_by=" not in details
    assert stream.close_calls == 1


async def test_reasoning_event_is_diagnosed_without_recording_reasoning_content():
    stream = TrackedStream([
        {"type": "response.created", "response": _response("in_progress"), "sequence_number": 0},
        {
            "type": "response.output_item.added", "output_index": 0, "sequence_number": 1,
            "item": {"type": "reasoning", "id": "rs_fixture", "summary": [],
                     "encrypted_content": "PRIVATE_REASONING"},
        },
        {
            "type": "response.output_text.delta", "item_id": "msg_fixture", "output_index": 1,
            "content_index": 0, "delta": "PRIVATE_TEXT", "logprobs": [], "sequence_number": 2,
        },
        {"type": "error", "error": {"message": "failed", "code": 500}},
    ])
    async with _client(stream) as client:
        model = PlatformResponsesModel(model="fixture-model", openai_client=client)
        with pytest.raises(APIError) as caught:
            async for _ in model.stream_response(**_kwargs(), tracing=ModelTracing.DISABLED):
                pass
    details = model_error_details(caught.value)
    assert "stream_events=3" in details
    assert "retry_blocked_by=response.output_item.added" in details
    assert "PRIVATE" not in details


async def test_successful_stream_keeps_completed_response_request_id_and_closes():
    stream = TrackedStream(
        [
            {"type": "response.created", "response": _response("in_progress"), "sequence_number": 0},
            {"type": "response.completed", "response": _response(), "sequence_number": 1},
        ]
    )
    async with _client(stream) as client:
        model = PlatformResponsesModel(model="fixture-model", openai_client=client)
        events = [event async for event in model.stream_response(**_kwargs(), tracing=ModelTracing.DISABLED)]
        assert [event.type for event in events] == ["response.created", "response.completed"]
        assert events[-1].response.output[0].content[0].text == "OK"
        assert events[-1].response._request_id == "req_fixture"
        assert stream.close_calls == 1


@pytest.mark.parametrize("method", ["aclose", "close"])
async def test_explicit_stream_close_delegates_to_sdk_cleanup(method):
    stream = TrackedStream(
        [
            {"type": "response.created", "response": _response("in_progress"), "sequence_number": 0},
        ]
    )
    async with _client(stream) as client:
        model = PlatformResponsesModel(model="fixture-model", openai_client=client)
        iterator = await model._fetch_response(**_kwargs(), stream=True)
        assert (await anext(iterator)).type == "response.created"
        await getattr(iterator, method)()
        assert stream.close_calls == 1


async def test_cancelled_stream_still_closes_http_response():
    stream = TrackedStream(
        [
            {"type": "response.created", "response": _response("in_progress"), "sequence_number": 0},
        ],
        block=True,
    )
    async with _client(stream) as client:
        model = PlatformResponsesModel(model="fixture-model", openai_client=client)

        async def consume():
            async for _ in model.stream_response(**_kwargs(), tracing=ModelTracing.DISABLED):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(stream.waiting.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stream.closed.wait(), timeout=2)
        assert stream.close_calls == 1
