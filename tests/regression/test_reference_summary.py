"""Complete-reference recovery using synthetic text and recording models only."""

from __future__ import annotations

import asyncio
import inspect

import pytest
from agents import Model, ModelResponse
from agents.exceptions import ModelBehaviorError
from agents.models.interface import ModelTracing
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from strixops.config.context import ContextSettings
from strixops.engine import reference_summary
from strixops.engine.model_capacity import ModelCapacity
from strixops.engine.resilience import MODEL_RETRY

MODEL = "synthetic/route-model"


class Overflow(Exception):
    pass


def response(text, *, status="completed"):
    return ModelResponse(
        output=[ResponseOutputMessage(
            id="synthetic-summary", type="message", role="assistant", status=status,
            content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
        )],
        usage=Usage(), response_id="synthetic-summary",
    )


class RecordingModel(Model):
    def __init__(self, produce=None):
        self.calls = []
        self.accepted = []
        self.produce = produce or (lambda _index, _request: response("Complete summary."))

    async def get_response(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        result = self.produce(index, kwargs)
        if inspect.isawaitable(result):
            result = await result
        self.accepted.append(kwargs)
        return result

    async def stream_response(self, **kwargs):
        raise AssertionError("Reference summaries must not stream")
        yield  # pragma: no cover


@pytest.fixture(autouse=True)
def offline_counter(monkeypatch):
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    monkeypatch.setattr(reference_summary, "count_tokens", lambda _model, text: len(text))
    monkeypatch.setattr(reference_summary, "is_context_overflow", lambda exc: isinstance(exc, Overflow))


def capacity(window=100_000, output=256, *, model=MODEL):
    return ModelCapacity(
        model=model, capacity_tokens=window, output_limit_tokens=output,
        capacity_source="provider_metadata", output_source="provider_metadata", lookup_status="resolved",
    )


async def summarize(text, model, *, routed=None, target=256, settings=None):
    return await reference_summary.summarize_reference(
        text, model=MODEL, summary_model=model,
        settings=settings or ContextSettings(summary_max_tokens=64),
        capacity=routed or capacity(), target_tokens=target,
    )


async def test_every_source_character_is_covered_in_order_with_same_route_and_no_tools():
    text = "HEAD\n" + "A" * 17_000 + "\nMIDDLE|`\n" + "B" * 17_000 + "\nTAIL\n"
    model = RecordingModel(lambda index, _request: response(f"Complete part {index}."))
    result = await summarize(text, model)
    assert result == "Complete part 0.\n\nComplete part 1.\n\nComplete part 2."
    assert "".join(call["input"] for call in model.accepted) == text
    assert all(len(call["input"]) <= 16_000 for call in model.calls)
    for call in model.calls:
        assert call["tools"] == [] and call["handoffs"] == [] and call["output_schema"] is None
        assert call["tracing"] is ModelTracing.DISABLED
        assert call["model_settings"].retry is MODEL_RETRY
        assert call["model_settings"].include_usage is True
        assert call["previous_response_id"] is None and call["conversation_id"] is None


async def test_length_retries_same_source_once_with_larger_bounded_output():
    def produce(index, _request):
        if index == 0:
            raise ModelBehaviorError(
                "Chat Completions response exceeded max_output_tokens (finish_reason=length)."
            )
        return response("Complete after retry.")

    model = RecordingModel(produce)
    text = "source" * 500
    assert await summarize(text, model, routed=capacity(output=128)) == "Complete after retry."
    assert [call["model_settings"].max_tokens for call in model.calls] == [64, 128]
    assert [call["input"] for call in model.calls] == [text, text]


async def test_repeated_length_splits_source_and_never_accepts_partial_output():
    def produce(_index, request):
        if len(request["input"]) > 1000:
            raise ModelBehaviorError("Responses output exceeded max_output_tokens.")
        return response("Complete smaller segment.")

    model = RecordingModel(produce)
    text = "A" * 1000 + "B" * 1000
    result = await summarize(text, model, routed=capacity(output=128))
    assert result == "Complete smaller segment.\n\nComplete smaller segment."
    assert [len(call["input"]) for call in model.calls] == [2000, 2000, 1000, 1000]
    assert "".join(call["input"] for call in model.accepted) == text


async def test_context_overflow_reduces_next_segment_size_without_losing_suffix():
    def produce(_index, request):
        if len(request["input"]) > 1000:
            raise Overflow("synthetic context overflow")
        return response("Complete segment.")

    model = RecordingModel(produce)
    text = "A" * 1000 + "B" * 1000
    assert await summarize(text, model) == "Complete segment.\n\nComplete segment."
    assert [len(call["input"]) for call in model.calls] == [2000, 1000, 1000]
    assert "".join(call["input"] for call in model.accepted) == text


async def test_output_at_route_ceiling_splits_without_exceeding_ceiling():
    def produce(_index, request):
        if len(request["input"]) > 1000:
            raise ModelBehaviorError("finish_reason=length")
        return response("Complete.")

    model = RecordingModel(produce)
    assert await summarize("X" * 2000, model, routed=capacity(output=32)) == "Complete.\n\nComplete."
    assert [len(call["input"]) for call in model.calls] == [2000, 1000, 1000]
    assert all(call["model_settings"].max_tokens == 32 for call in model.calls)


async def test_output_increase_is_capped_by_remaining_context():
    def produce(index, _request):
        if index == 0:
            raise ModelBehaviorError("finish_reason=length")
        return response("Complete within context.")

    window = 4096
    overhead = len(reference_summary._INSTRUCTIONS) + reference_summary._FRAMING_TOKENS
    text = "X" * (window - overhead - 100)
    model = RecordingModel(produce)
    assert await summarize(text, model, routed=capacity(window, output=256)) == "Complete within context."
    assert [call["model_settings"].max_tokens for call in model.calls] == [64, 100]
    assert model.calls[0]["input"] == model.calls[1]["input"] == text


async def test_all_complete_segment_summaries_are_included_in_one_merge(monkeypatch):
    monkeypatch.setattr(reference_summary, "_MAX_CHUNK_TOKENS", 2000)
    parts = ["first fact " * 15, "second fact " * 15]

    def produce(index, _request):
        return response(parts[index] if index < 2 else "Both distinct facts, fully merged.")

    model = RecordingModel(produce)
    assert await summarize("A" * 4000, model, target=200) == "Both distinct facts, fully merged."
    assert len(model.calls) == 3
    assert model.calls[-1]["input"] == "\n\n".join(part.strip() for part in parts)
    assert model.calls[-1]["system_instructions"] == reference_summary._MERGE_INSTRUCTIONS


async def test_merge_length_retry_can_complete_without_using_rejected_output(monkeypatch):
    monkeypatch.setattr(reference_summary, "_MAX_CHUNK_TOKENS", 1000)

    def produce(index, _request):
        if index == 2:
            raise ModelBehaviorError("Responses output exceeded max_output_tokens.")
        return response("S" * 100 if index < 2 else "Complete merged facts.")

    model = RecordingModel(produce)
    assert await summarize("A" * 2000, model, target=150) == "Complete merged facts."
    assert len(model.calls) == 4
    assert model.calls[2]["input"] == model.calls[3]["input"]
    assert model.calls[3]["model_settings"].max_tokens == 128


@pytest.mark.parametrize("error", [ConnectionError("synthetic network failure"), TimeoutError(),
                                  ModelBehaviorError("response was refused (content_filter)")])
async def test_network_or_unrelated_model_errors_return_none_without_local_retry(error):
    def produce(_index, _request):
        raise error

    model = RecordingModel(produce)
    assert await summarize("synthetic source " * 100, model) is None
    assert len(model.calls) == 1


@pytest.mark.parametrize("status", ["incomplete", "in_progress"])
async def test_incomplete_message_is_not_a_usable_summary(status):
    model = RecordingModel(lambda _index, _request: response("Partial text must not escape.", status=status))
    assert await summarize("source " * 100, model) is None
    assert len(model.calls) == 1


async def test_empty_summary_is_not_accepted():
    model = RecordingModel(lambda _index, _request: response(" \n "))
    assert await summarize("source " * 100, model) is None


async def test_retry_limit_is_eight_even_when_every_response_hits_length():
    def produce(_index, _request):
        raise ModelBehaviorError("finish_reason=length")

    model = RecordingModel(produce)
    assert await summarize("source " * 5000, model, routed=capacity(output=32)) is None
    assert len(model.calls) == 8


async def test_uncovered_source_returns_none_even_with_good_partial_summaries(monkeypatch):
    monkeypatch.setattr(reference_summary, "_MAX_CHUNK_TOKENS", 100)
    model = RecordingModel()
    assert await summarize("source" * 150, model) is None
    assert len(model.calls) == 8
    assert sum(len(call["input"]) for call in model.calls) == 800


async def test_no_shrink_after_complete_merge_returns_none():
    model = RecordingModel(lambda _index, request: response(request["input"]))
    assert await summarize("source" * 100, model, target=200) is None
    assert len(model.calls) == 2


async def test_merge_overflow_returns_none_without_dropping_any_summaries(monkeypatch):
    monkeypatch.setattr(reference_summary, "_MAX_CHUNK_TOKENS", 1000)

    def produce(index, _request):
        if index == 2:
            raise Overflow("synthetic merge context overflow")
        return response(f"part {index}: " + "S" * 100)

    model = RecordingModel(produce)
    assert await summarize("source" * 300, model, target=150) is None
    assert "part 0:" in model.calls[-1]["input"] and "part 1:" in model.calls[-1]["input"]


@pytest.mark.parametrize("window", [2048, 4096, 8192])
async def test_every_request_respects_capacity_and_route_output(window):
    model = RecordingModel(lambda _index, _request: response("Done."))
    assert await summarize("X" * 1200, model, routed=capacity(window, output=32)) is not None
    for call in model.calls:
        allowance = call["model_settings"].max_tokens
        used = len(call["system_instructions"]) + len(call["input"]) + allowance
        assert used + reference_summary._FRAMING_TOKENS <= window
        assert allowance <= 32


async def test_impossible_window_returns_none_before_request():
    model = RecordingModel()
    assert await summarize("source " * 100, model, routed=capacity(512)) is None
    assert model.calls == []


async def test_different_model_capacity_uses_configured_default_instead():
    model = RecordingModel()
    assert await summarize(
        "source " * 100, model, routed=capacity(128, model="other/model"),
        settings=ContextSettings(fallback_context_tokens=4096, summary_max_tokens=64),
    ) == "Complete summary."


async def test_cancellation_propagates():
    def produce(_index, _request):
        raise asyncio.CancelledError

    model = RecordingModel(produce)
    with pytest.raises(asyncio.CancelledError):
        await summarize("source " * 100, model)
    assert len(model.calls) == 1


@pytest.mark.parametrize(("configured", "expected"), [
    (None, {"timeout": 300.0}), ("1.75", {"timeout": 1.75}), ("0", None), ("-1", None),
])
async def test_timeout_extra_args_follow_existing_convention(monkeypatch, configured, expected):
    if configured is not None:
        monkeypatch.setenv("LLM_TIMEOUT", configured)

    async def produce(_index, _request):
        await asyncio.sleep(0.01)
        return response("Complete summary.")

    model = RecordingModel(produce)
    assert await summarize("source " * 100, model) == "Complete summary."
    assert model.calls[0]["model_settings"].extra_args == expected


async def test_logical_timeout_cancels_pending_response_and_returns_none(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT", "0.01")
    cancelled = asyncio.Event()

    async def produce(_index, _request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    model = RecordingModel(produce)
    assert await asyncio.wait_for(summarize("source " * 100, model), timeout=1) is None
    assert cancelled.is_set()
    assert len(model.calls) == 1 and model.accepted == []
    assert model.calls[0]["model_settings"].extra_args == {"timeout": 0.01}


@pytest.mark.parametrize("configured", ["300", "0"])
async def test_external_cancellation_while_awaiting_response_propagates(monkeypatch, configured):
    monkeypatch.setenv("LLM_TIMEOUT", configured)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def produce(_index, _request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    model = RecordingModel(produce)
    task = asyncio.create_task(summarize("source " * 100, model))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert len(model.calls) == 1 and model.accepted == []
