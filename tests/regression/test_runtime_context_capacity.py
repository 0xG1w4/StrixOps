"""Run-scoped compaction limits, using only local SQLite and a recording model."""

from __future__ import annotations

import os
from typing import Any

import pytest
from agents import Agent, Model, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from strixops.config.context import ContextSettings
from strixops.engine import compaction, loop
from strixops.engine.coordinator import AgentCoordinator, CoordinationLimits
from strixops.engine.model_capacity import ModelCapacity
from strixops.engine.scanconfig import EngineContext, EngineServices, LifecycleCompletion
from strixops.engine.sessions import open_agent_session
from strixops.platform.events import EventWriter

MODEL = "vendor/scan-model"


class MockModel(Model):
    def __init__(self, model: str = MODEL) -> None:
        self.model = model
        self.calls: list[dict[str, Any]] = []

    async def get_response(self, **kwargs: Any) -> ModelResponse:
        self.calls.append(kwargs)
        # Use the full allowance: the rewritten context must fit even when the
        # summary does not happen to be shorter than its requested limit.
        summary = "S" * kwargs["model_settings"].max_tokens
        return ModelResponse(
            output=[ResponseOutputMessage(
                id="summary", type="message", role="assistant", status="completed",
                content=[ResponseOutputText(type="output_text", text=summary, annotations=[])],
            )],
            usage=Usage(), response_id="summary",
        )

    async def stream_response(self, **kwargs: Any):
        raise AssertionError("These tests must not request a model stream")
        yield  # pragma: no cover -- retain the SDK's async-generator interface


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in tuple(os.environ):
        if name.upper().startswith(("STRIX_", "STRIXOPS_", "LLM_")):
            monkeypatch.delenv(name, raising=False)
    # A deterministic, conservative counter makes budget boundaries explicit
    # and avoids tokenizer/catalog initialization or any associated network IO.
    monkeypatch.setattr(compaction, "count_tokens", lambda _model, text: len(text))


@pytest.fixture
def session(tmp_path):
    value = open_agent_session("root", tmp_path / "agents.db")
    try:
        yield value
    finally:
        value.close()


def capacity(window=8192, output=256, *, model=MODEL):
    return ModelCapacity(
        model=model, capacity_tokens=window, output_limit_tokens=output,
        capacity_source="provider_metadata", output_source="provider_metadata", lookup_status="resolved",
    )


def history(count=12, width=1000):
    return [{"role": "user", "content": f"message-{i}: " + "x" * width} for i in range(count)]


def prohibit_catalog(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("A matching run capacity must bypass model-name metadata")

    monkeypatch.setattr(compaction, "context_window", fail)
    monkeypatch.setattr(compaction, "output_limit", fail)


@pytest.mark.parametrize(
    ("window", "output", "expected"),
    [(200_000, 8192, 180_000), (8192, 8192, 4096), (4096, 32768, 2048), (1, 8192, 0)],
)
def test_published_budget_respects_small_windows(window, output, expected):
    settings = ContextSettings(keep_tokens=1_000_000)
    assert compaction.compaction_budget(capacity(window, output), settings) == expected
    assert expected < window


@pytest.mark.parametrize("window", [4096, 8192, 32_768])
async def test_route_limits_bound_summary_and_rewritten_context(session, monkeypatch, window):
    prohibit_catalog(monkeypatch)
    await session.add_items(history(count=40))
    routed = capacity(window, output=32768)
    settings = ContextSettings(keep_tokens=1_000_000)
    model = MockModel()
    instructions, tools = "fixed instructions " * 10, "tool schema " * 10

    assert await compaction.maybe_compact(
        session, model=MODEL, summary_model=model, instructions=instructions,
        tools_text=tools, settings=settings, capacity=routed,
    )

    request = model.calls[0]
    output = request["model_settings"].max_tokens
    assert output <= min(settings.summary_max_tokens, routed.output_limit_tokens, window // 4)
    assert len(request["input"]) + output <= window
    assert request["tools"] == []
    rewritten = await session.get_items()
    used = len("\n".join((instructions, tools, compaction._serialize_items(rewritten))))
    assert used <= compaction.compaction_budget(routed, settings)
    assert rewritten[0]["content"].startswith("<conversation-checkpoint>")
    assert len(rewritten) < 40


async def test_provider_output_limit_is_used_by_summary(session, monkeypatch):
    prohibit_catalog(monkeypatch)
    await session.add_items(history())
    model = MockModel()
    assert await compaction.maybe_compact(
        session, model=MODEL, summary_model=model, capacity=capacity(output=64),
    )
    assert model.calls[0]["model_settings"].max_tokens == 64


async def test_forced_summary_accounts_for_previous_checkpoint(session, monkeypatch):
    prohibit_catalog(monkeypatch)
    previous = compaction._checkpoint_item("previous facts " * 100)
    await session.add_items([previous, *history()])
    model = MockModel()
    routed = capacity(output=512)
    settings = ContextSettings(auto_compact=False)
    assert not await compaction.maybe_compact(
        session, model=MODEL, summary_model=model, capacity=routed, settings=settings,
    )
    assert await compaction.maybe_compact(
        session, model=MODEL, summary_model=model, capacity=routed, settings=settings, force=True,
    )
    request = model.calls[0]
    assert previous["content"] in request["input"]
    assert len(request["input"]) + request["model_settings"].max_tokens <= routed.capacity_tokens
    used = len("\n\n" + compaction._serialize_items(await session.get_items()))
    assert used <= compaction.compaction_budget(routed, settings)


async def test_published_budget_is_actual_trigger_boundary(session, monkeypatch):
    prohibit_catalog(monkeypatch)
    items = history(count=6)
    await session.add_items(items)
    used = len("\n\n" + compaction._serialize_items(items))
    settings = ContextSettings(compact_buffer_tokens=100, keep_tokens=10, summary_max_tokens=32)
    model = MockModel()
    exact = capacity(used + 100, output=32)
    assert compaction.compaction_budget(exact, settings) == used
    assert not await compaction.maybe_compact(
        session, model=MODEL, summary_model=model, settings=settings, capacity=exact,
    )
    assert not model.calls
    assert await session.get_items() == items

    smaller = capacity(used + 99, output=32)
    assert compaction.compaction_budget(smaller, settings) == used - 1
    assert await compaction.maybe_compact(
        session, model=MODEL, summary_model=model, settings=settings, capacity=smaller,
    )
    assert len(model.calls) == 1


async def test_oversized_tool_group_is_summarized_as_a_complete_pair(session, monkeypatch):
    prohibit_catalog(monkeypatch)
    items = [
        *history(count=6),
        {"type": "function_call", "call_id": "large", "name": "tool", "arguments": "x" * 1900},
        {"type": "function_call_output", "call_id": "large", "output": "small result"},
        {"role": "user", "content": "most recent instruction"},
    ]
    await session.add_items(items)
    model = MockModel()
    routed = capacity(4096, output=128)
    assert await compaction.maybe_compact(
        session, model=MODEL, summary_model=model, capacity=routed,
    )
    result = await session.get_items()
    assert result[-1] == items[-1]
    assert all(item.get("call_id") != "large" for item in result)
    assert len("\n\n" + compaction._serialize_items(result)) <= compaction.compaction_budget(
        routed, ContextSettings()
    )


@pytest.mark.parametrize(("window", "instructions"), [(1, ""), (1024, ""), (8192, "x" * 5000)])
async def test_impossible_summary_preserves_history_without_model_call(
    session, monkeypatch, window, instructions,
):
    prohibit_catalog(monkeypatch)
    items = history()
    await session.add_items(items)
    model = MockModel()
    assert not await compaction.maybe_compact(
        session, model=MODEL, summary_model=model, instructions=instructions,
        capacity=capacity(window), force=True,
    )
    assert not model.calls
    assert await session.get_items() == items


async def test_fixed_prompt_reduces_summary_allowance_before_history(session, monkeypatch):
    prohibit_catalog(monkeypatch)
    items = history()
    await session.add_items(items)
    model = MockModel()
    instructions = "x" * 3400
    routed = capacity(output=8192)
    assert await compaction.maybe_compact(
        session, model=MODEL, summary_model=model, instructions=instructions, capacity=routed,
    )
    assert model.calls[0]["model_settings"].max_tokens < 2048
    rewritten = await session.get_items()
    used = len("\n".join((instructions, "", compaction._serialize_items(rewritten))))
    assert used <= compaction.compaction_budget(routed, ContextSettings())


@pytest.mark.parametrize("runtime_capacity", [None, capacity(model="vendor/other-scan-model")])
async def test_legacy_and_different_model_calls_use_their_own_metadata(
    session, monkeypatch, runtime_capacity,
):
    monkeypatch.setattr(compaction, "context_window", lambda *args: 200_000)
    monkeypatch.setattr(compaction, "output_limit", lambda *args: 123)
    items = history()
    await session.add_items(items)
    model = MockModel()
    assert not await compaction.maybe_compact(
        session, model=MODEL, summary_model=model, capacity=runtime_capacity,
    )
    assert not model.calls
    assert await session.get_items() == items
    assert compaction._summary_output_tokens(MODEL, capacity=runtime_capacity) == 123


async def test_compact_session_keeps_legacy_call_signature(session, monkeypatch):
    calls = []

    async def legacy_maybe_compact(
        session, *, model, summary_model, instructions, tools_text, force, settings,
    ):
        calls.append((model, force))
        return True

    monkeypatch.setattr(compaction, "maybe_compact", legacy_maybe_compact)
    agent = Agent(name="root", model=MockModel())
    assert await loop._compact_session(agent, session, ContextSettings(), force=True)
    assert calls == [(MODEL, True)]


async def test_root_and_child_share_capacity_in_proactive_and_overflow_paths(tmp_path, monkeypatch):
    routed = capacity()
    events = EventWriter(tmp_path)
    coordinator = AgentCoordinator(tmp_path, events, limits=CoordinationLimits())
    services = EngineServices(context_settings=ContextSettings(), model_capacity=routed)
    seen = []

    async def recording_compaction(session, *, capacity, force, **kwargs):
        seen.append((session.session_id, capacity, force))
        return force

    class Overflow(Exception):
        pass

    class Stream:
        raw_responses = []

        def __init__(self, failure):
            self.failure = failure

        async def stream_events(self):
            if self.failure:
                raise Overflow()
            for event in ():
                yield event

    attempts = {}

    def run_streamed(agent, *, context, **kwargs):
        attempt = attempts.get(context.agent_id, 0)
        attempts[context.agent_id] = attempt + 1
        if attempt:
            root = context.parent_id is None
            context.lifecycle_completion = LifecycleCompletion(
                "finish_scan" if root else "agent_finish",
                {"success": True, "scan_completed" if root else "agent_finished": True},
            )
        return Stream(failure=not attempt)

    monkeypatch.setattr(compaction, "maybe_compact", recording_compaction)
    monkeypatch.setattr(compaction, "is_context_overflow", lambda exc: isinstance(exc, Overflow))
    monkeypatch.setattr(loop.Runner, "run_streamed", run_streamed)
    for agent_id, parent in (("root", None), ("child", "root")):
        await coordinator.register(agent_id, agent_id, parent_id=parent, task="local test")
        context = EngineContext(agent_id=agent_id, parent_id=parent, services=services)
        model = MockModel()
        result = await loop.run_agent_loop(
            Agent(name=agent_id, model=model), context, initial_input="local instruction",
            coordinator=coordinator, events=events,
        )
        assert result["success"]
        assert not model.calls
    assert [(agent_id, forced) for agent_id, _, forced in seen] == [
        ("root", False), ("root", True), ("root", False),
        ("child", False), ("child", True), ("child", False),
    ]
    assert all(value is routed for _, value, _ in seen)
