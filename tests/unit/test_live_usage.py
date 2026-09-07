"""Run usage is observable as each model response completes, including on failure."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from agents import Model, ModelResponse, ModelSettings, ModelTracing
from agents.items import Usage
from openai.types.responses import Response, ResponseCompletedEvent, ResponseTextDeltaEvent

from strixops.engine.usage import UsageAccumulator, UsageTrackingModel
from strixops.testing.scripted_gateway import completed_stream_event


def _response(input_tokens: int = 100, output_tokens: int = 20) -> ModelResponse:
    return ModelResponse(
        output=[],
        response_id="resp-live-usage",
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


def _delta() -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent(
        content_index=0,
        delta="Checking the next endpoint.",
        item_id="msg-live-usage",
        logprobs=[],
        output_index=0,
        sequence_number=0,
        type="response.output_text.delta",
    )


def _completed_without_usage() -> ResponseCompletedEvent:
    return ResponseCompletedEvent(
        response=Response(
            id="resp-no-usage",
            created_at=0,
            model="fake",
            object="response",
            output=[],
            tool_choice="auto",
            tools=[],
            parallel_tool_calls=False,
            usage=None,
        ),
        sequence_number=1,
        type="response.completed",
    )


class _FakeModel(Model):
    def __init__(
        self,
        response: ModelResponse | None = None,
        *,
        events: list[Any] | None = None,
        stream_error: BaseException | None = None,
        on_start: Callable[[], None] | None = None,
    ) -> None:
        self.response = response or _response()
        self.events = events if events is not None else [completed_stream_event(self.response, "fake")]
        self.stream_error = stream_error
        self.on_start = on_start
        self.calls = 0
        self.stream_ended = False

    @property
    def model(self) -> str:
        return "fake"

    async def close(self) -> None:
        pass

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        if self.on_start:
            self.on_start()
        return self.response

    async def stream_response(self, *args: Any, **kwargs: Any):
        self.calls += 1
        if self.on_start:
            self.on_start()
        for event in self.events:
            yield event
        self.stream_ended = True
        if self.stream_error:
            raise self.stream_error


def _stream(model: UsageTrackingModel):
    return model.stream_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
    )


async def _get_response(model: UsageTrackingModel) -> ModelResponse:
    return await model.get_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
    )


@pytest.mark.asyncio
async def test_stream_publishes_before_terminal_event_reaches_consumer_and_next_call():
    updates = []
    accumulator = UsageAccumulator(on_update=updates.append)
    terminal = completed_stream_event(_response(), "fake")
    delta = _delta()
    inner = _FakeModel(events=[delta, terminal])
    stream = _stream(UsageTrackingModel(inner, accumulator))

    assert await anext(stream) is delta
    assert updates == []
    assert await anext(stream) is terminal
    assert not inner.stream_ended
    assert updates == [{"requests": 1, "input_tokens": 100, "output_tokens": 20, "total_tokens": 120}]

    def next_call_starts():
        assert updates[-1]["requests"] == 1

    next_model = _FakeModel(_response(50, 10), on_start=next_call_starts)
    await _get_response(UsageTrackingModel(next_model, accumulator))
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    accumulator.flush()
    assert len(updates) == 2
    assert updates[-1] == {
        "requests": 2,
        "input_tokens": 150,
        "output_tokens": 30,
        "total_tokens": 180,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("stream lost"), asyncio.CancelledError()])
async def test_completed_usage_survives_stream_error_or_cancellation(error):
    updates = []
    accumulator = UsageAccumulator(on_update=updates.append)
    inner = _FakeModel(stream_error=error)

    with pytest.raises(type(error)):
        async for _ in _stream(UsageTrackingModel(inner, accumulator)):
            assert updates[-1]["total_tokens"] == 120

    accumulator.flush()
    assert inner.calls == 1
    assert updates == [{"requests": 1, "input_tokens": 100, "output_tokens": 20, "total_tokens": 120}]
    assert accumulator.snapshot() == updates[-1]


@pytest.mark.asyncio
async def test_shared_accumulator_combines_parent_and_child_responses_once():
    updates = []
    accumulator = UsageAccumulator(on_update=updates.append)
    parent = UsageTrackingModel(_FakeModel(_response(100, 10)), accumulator)
    children = [
        UsageTrackingModel(_FakeModel(_response(200, 15)), accumulator),
        UsageTrackingModel(_FakeModel(_response(300, 25)), accumulator),
    ]
    await _get_response(parent)

    async def consume(model):
        async for _ in _stream(model):
            pass
        accumulator.flush()

    await asyncio.gather(*(consume(child) for child in children))
    accumulator.flush()
    assert [update["requests"] for update in updates] == [1, 2, 3]
    assert updates[0] == {"requests": 1, "input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    assert updates[-1] == {"requests": 3, "input_tokens": 600, "output_tokens": 50, "total_tokens": 650}
    assert accumulator.snapshot() == updates[-1]


@pytest.mark.asyncio
async def test_failed_publication_is_logged_and_flush_retries_without_repeating_model(caplog):
    attempts = []
    published = []

    def publish(snapshot):
        attempts.append(snapshot)
        if len(attempts) == 1:
            raise OSError("run-state unavailable")
        published.append(snapshot)

    accumulator = UsageAccumulator(on_update=publish)
    inner = _FakeModel()
    response = await _get_response(UsageTrackingModel(inner, accumulator))
    assert response is inner.response
    assert inner.calls == 1
    assert published == []
    assert "run-state unavailable" in caplog.text
    assert accumulator.snapshot()["total_tokens"] == 120

    accumulator.flush()
    accumulator.flush()
    assert len(attempts) == 2
    assert published == [{"requests": 1, "input_tokens": 100, "output_tokens": 20, "total_tokens": 120}]
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_nonstreaming_calls_publish_cumulative_usage_before_returning():
    updates = []
    accumulator = UsageAccumulator(on_update=updates.append)
    model = UsageTrackingModel(_FakeModel(_response(60, 5)), accumulator)

    await _get_response(model)
    assert updates == [{"requests": 1, "input_tokens": 60, "output_tokens": 5, "total_tokens": 65}]
    await _get_response(model)
    assert updates[-1] == {"requests": 2, "input_tokens": 120, "output_tokens": 10, "total_tokens": 130}
    accumulator.flush()
    assert len(updates) == 2


@pytest.mark.asyncio
async def test_completed_stream_without_provider_usage_counts_request_without_inventing_tokens():
    updates = []
    accumulator = UsageAccumulator(on_update=updates.append)
    inner = _FakeModel(events=[_delta(), _completed_without_usage()])

    async for _ in _stream(UsageTrackingModel(inner, accumulator)):
        pass
    accumulator.flush()
    assert updates == [{"requests": 1, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}]


@pytest.mark.asyncio
async def test_incomplete_stream_does_not_invent_completed_requests_or_tokens():
    updates = []
    accumulator = UsageAccumulator(on_update=updates.append)
    inner = _FakeModel(events=[_delta()], stream_error=RuntimeError("connection dropped"))

    with pytest.raises(RuntimeError, match="connection dropped"):
        async for _ in _stream(UsageTrackingModel(inner, accumulator)):
            pass
    accumulator.flush()
    assert accumulator.snapshot() == {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    assert all(snapshot["requests"] == 0 and snapshot["total_tokens"] == 0 for snapshot in updates)
