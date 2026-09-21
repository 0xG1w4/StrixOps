"""Context-size telemetry must remain request-local and leave inference intact."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from agents import Agent, Model, ModelSettings, ModelTracing
from agents.agent_output import AgentOutputSchema
from agents.handoffs import Handoff
from agents.sandbox import SandboxAgent
from agents.tool import FunctionTool
from agents.usage import Usage
from pydantic import BaseModel, Field

from strixops.engine import context_usage, loop, resilience
from strixops.engine.context_usage import ContextUsageModel
from strixops.engine.coordinator import AgentCoordinator, CoordinationLimits
from strixops.engine.model_capacity import ModelCapacity
from strixops.engine.scanconfig import EngineContext, EngineServices, LifecycleCompletion
from strixops.engine.usage import UsageAccumulator, UsageTrackingModel
from strixops.platform.events import EventWriter

MODEL = "vendor/main-model"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.upper().startswith(("STRIX_", "STRIXOPS_", "LLM_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(context_usage, "count_tokens", lambda _model, text: len(text))


def request(text="main input", **overrides):
    return {
        "system_instructions": "fixed instructions",
        "input": text,
        "model_settings": ModelSettings(),
        "tools": [],
        "output_schema": None,
        "handoffs": [],
        "tracing": ModelTracing.DISABLED,
        "previous_response_id": None,
        "conversation_id": None,
        "prompt": None,
        **overrides,
    }


def response(tokens=500):
    return SimpleNamespace(usage=SimpleNamespace(input_tokens=tokens))


def completed(value):
    return SimpleNamespace(type="response.completed", response=value)


class MockModel(Model):
    model = MODEL

    def __init__(self, result=None, *, handler=None, stream_handler=None):
        self.result = result if result is not None else response()
        self.handler = handler
        self.stream_handler = stream_handler
        self.calls = []
        self.stream_closed = False
        self.closed = False
        self.advice = None
        self.advice_request = None

    async def get_response(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.handler is not None:
            return await self.handler(*args, **kwargs)
        return self.result

    async def stream_response(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        try:
            if self.stream_handler is not None:
                async for event in self.stream_handler(*args, **kwargs):
                    yield event
            else:
                yield completed(self.result)
        finally:
            self.stream_closed = True

    def get_retry_advice(self, request):
        self.advice_request = request
        return self.advice

    async def close(self):
        self.closed = True


def tracked(inner, snapshots, agent_id="root", agent_name="Root"):
    return ContextUsageModel(
        inner, agent_id=agent_id, agent_name=agent_name, on_update=snapshots.append,
    )


async def invoke(model, mode, **kwargs):
    if mode == "stream":
        return [event async for event in model.stream_response(**request(**kwargs))]
    return await model.get_response(**request(**kwargs))


@pytest.mark.parametrize("mode", ["get", "stream"])
async def test_full_provider_input_corrects_estimate_without_subtracting_cache(mode):
    snapshots = []
    value = SimpleNamespace(usage=SimpleNamespace(
        input_tokens=900, input_tokens_details=SimpleNamespace(cached_tokens=800),
    ))
    inner = MockModel(value)
    model = tracked(inner, snapshots)
    await invoke(model, mode)
    assert [item["phase"] for item in snapshots] == ["request", "response"]
    assert [item["source"] for item in snapshots] == ["estimate", "provider_usage"]
    assert snapshots[0]["input_tokens"] > 0
    assert snapshots[-1]["input_tokens"] == 900
    assert snapshots[-1]["agent_name"] == "Root"
    assert snapshots[-1]["model"] == MODEL
    assert datetime.fromisoformat(snapshots[-1]["updated_at"]).utcoffset() == UTC.utcoffset(None)
    assert set(snapshots[-1]) == {"agent_name", "model", "input_tokens", "source", "phase", "updated_at"}
    assert inner.calls[0][0][1] == "main input"


@pytest.mark.parametrize("mode", ["get", "stream"])
@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": True}, {"input_tokens": -1}, Usage()])
async def test_absent_or_invalid_usage_keeps_the_estimate(mode, usage):
    snapshots = []
    model = tracked(MockModel(SimpleNamespace(usage=usage)), snapshots)
    await invoke(model, mode)
    assert snapshots[-1]["phase"] == "response"
    assert snapshots[-1]["source"] == "estimate"
    assert snapshots[-1]["input_tokens"] == snapshots[0]["input_tokens"] > 0


@pytest.mark.parametrize("usage", [Usage(requests=1, input_tokens=321), Usage(requests=1, input_tokens=0)])
async def test_sdk_usage_with_a_recorded_request_is_authoritative(usage):
    snapshots = []
    await invoke(tracked(MockModel(SimpleNamespace(usage=usage)), snapshots), "get")
    assert snapshots[-1]["source"] == "provider_usage"
    assert snapshots[-1]["input_tokens"] == usage.input_tokens


async def test_later_requests_replace_instead_of_adding_usage():
    snapshots = []
    inner = MockModel(response(500))
    model = tracked(inner, snapshots)
    await invoke(model, "get")
    inner.result = response(90)
    await invoke(model, "get", text="new request after compaction")
    assert [item["input_tokens"] for item in snapshots if item["source"] == "provider_usage"] == [500, 90]


async def test_estimator_sees_complete_history_tool_schemas_handoffs_and_output_schema(monkeypatch):
    serialized = []

    def record_text(model, text):
        serialized.append(json.loads(text))
        return len(text)

    monkeypatch.setattr(context_usage, "count_tokens", record_text)
    long_text = "PRIVATE_" + "A" * 20_000 + "_HISTORY_END"
    tool_description = "T" * 6000 + "_TOOL_END"
    handoff_description = "H" * 4000 + "_HANDOFF_END"
    schema_description = "D" * 5000 + "_SCHEMA_END"

    async def unused(*args):
        raise AssertionError("Counting a request must not execute tools or handoffs")

    tool = FunctionTool(
        name="large_tool", description=tool_description,
        params_json_schema={"type": "object", "properties": {"data": {"type": "string"}}},
        on_invoke_tool=unused,
    )
    handoff = Handoff(
        tool_name="transfer", tool_description=handoff_description,
        input_json_schema={"type": "object", "properties": {"target": {"type": "string"}}},
        on_invoke_handoff=unused, agent_name="Other",
    )

    class Output(BaseModel):
        text: str = Field(description=schema_description)

    items = [
        {"type": "function_call", "call_id": "x", "name": "large_tool", "arguments": long_text},
        {"type": "function_call_output", "call_id": "x", "output": long_text},
        {"type": "reasoning", "encrypted_content": long_text, "summary": []},
        {"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]},
    ]
    snapshots = []
    await invoke(
        tracked(MockModel(), snapshots), "get", text=items, system_instructions=long_text,
        tools=[tool], handoffs=[handoff], output_schema=AgentOutputSchema(Output),
        prompt={"id": "pmpt_fixture", "variables": {"message": long_text}},
    )
    payload = serialized[0]
    assert payload["instructions"] == long_text
    assert payload["input"] == items
    assert payload["tools_and_handoffs"][0]["description"] == tool_description
    assert payload["tools_and_handoffs"][1]["description"] == handoff_description
    assert payload["output_schema"]["schema"]["properties"]["text"]["description"] == schema_description
    assert payload["prompt"]["variables"]["message"] == long_text
    assert "PRIVATE_" not in json.dumps(snapshots)
    assert snapshots[0]["input_tokens"] > 80_000


async def test_shared_inner_model_keeps_concurrent_agent_snapshots_independent():
    arrived = 0
    ready = asyncio.Event()

    async def handle(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await ready.wait()
        return response(100 if args[1] == "root" else 200)

    inner = MockModel(handler=handle)
    root, child = [], []
    await asyncio.gather(
        invoke(tracked(inner, root), "get", text="root"),
        invoke(tracked(inner, child, "child", "Child"), "get", text="child"),
    )
    assert root[-1]["input_tokens"] == 100
    assert child[-1]["input_tokens"] == 200
    assert {value["agent_name"] for value in root} == {"Root"}
    assert {value["agent_name"] for value in child} == {"Child"}


async def test_late_older_response_cannot_replace_newer_request():
    started, release = asyncio.Event(), asyncio.Event()

    async def handle(*args, **kwargs):
        if args[1] == "old":
            started.set()
            await release.wait()
            return response(999)
        return response(12)

    snapshots = []
    model = tracked(MockModel(handler=handle), snapshots)
    old = asyncio.create_task(invoke(model, "get", text="old"))
    await started.wait()
    await invoke(model, "get", text="new")
    release.set()
    await old
    assert snapshots[-1]["input_tokens"] == 12
    assert not any(item["input_tokens"] == 999 for item in snapshots)


@pytest.mark.parametrize("mode", ["get", "stream"])
async def test_estimator_and_storage_failures_cannot_fail_model_calls(mode, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic diagnostics failure")

    inner = MockModel(response(123))
    model = ContextUsageModel(inner, agent_id="root", agent_name="Root", on_update=fail)
    await invoke(model, mode)
    monkeypatch.setattr(context_usage, "estimate_context_tokens", fail)
    await invoke(model, mode)
    assert len(inner.calls) == 2


async def test_stream_errors_keep_usage_tracking_retry_provenance():
    failure = RuntimeError("interrupted stream")

    async def broken(*args, **kwargs):
        raise failure
        yield

    inner = MockModel(stream_handler=broken)
    inner.advice = SimpleNamespace(suggested=False, replay_safety="unsafe")
    snapshots = []
    wrapped = tracked(UsageTrackingModel(inner, UsageAccumulator()), snapshots)
    with pytest.raises(RuntimeError) as caught:
        await invoke(wrapped, "stream")
    assert caught.value is failure
    provenance = resilience.model_stream_failure(failure)
    assert provenance is not None
    retry = SimpleNamespace(error=failure, attempt=2, previous_response_id=None, conversation_id=None)
    assert wrapped.get_retry_advice(retry) is inner.advice
    assert inner.advice_request is retry
    assert provenance.request_attempt == 2
    assert provenance.provider_veto
    assert inner.stream_closed
    assert [item["phase"] for item in snapshots] == ["request"]


@pytest.mark.parametrize("mode", ["get", "stream"])
async def test_cancellation_propagates_and_closes_stream(mode):
    started = asyncio.Event()
    finished = asyncio.Event()

    async def waiting(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async def waiting_stream(*args, **kwargs):
        await waiting()
        yield

    snapshots = []
    inner = MockModel(handler=waiting, stream_handler=waiting_stream)
    task = asyncio.create_task(invoke(tracked(inner, snapshots), mode))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert [item["phase"] for item in snapshots] == ["request"]
    if mode == "stream":
        assert inner.stream_closed


async def test_early_stream_close_closes_inner_iterator_and_model_close_delegates():
    async def partial(*args, **kwargs):
        yield SimpleNamespace(type="response.output_text.delta", delta="partial")
        await asyncio.Event().wait()

    snapshots = []
    inner = MockModel(stream_handler=partial)
    model = tracked(inner, snapshots)
    stream = model.stream_response(**request())
    await anext(stream)
    await stream.aclose()
    assert inner.stream_closed
    assert [item["phase"] for item in snapshots] == ["request"]
    await model.close()
    assert inner.closed


def route(model=MODEL):
    return ModelCapacity(model, 8192, 1024, "provider_metadata", "provider_metadata", "resolved")


class State:
    def __init__(self):
        self.updates = []

    def record_context_usage(self, agent_id, snapshot):
        self.updates.append((agent_id, snapshot))


@pytest.mark.parametrize("kind", [Agent, SandboxAgent])
def test_agent_clone_preserves_subtype_settings_and_original_model(kind):
    inner = MockModel()
    settings = ModelSettings(temperature=0.3, max_tokens=1024)
    original = kind(name="Root", model=inner, model_settings=settings)
    clone = loop._context_tracked_agent(original, EngineContext(run_state=State()), route())
    assert type(clone) is kind
    assert clone is not original
    assert clone.model_settings == settings
    assert isinstance(clone.model, ContextUsageModel)
    assert original.model is inner


@pytest.mark.parametrize("case", ["no_capacity", "different_model", "no_state", "legacy_state"])
def test_missing_telemetry_or_different_model_keeps_legacy_agent(case):
    agent = Agent(name="Root", model=MockModel())
    context = EngineContext(run_state=State())
    capacity = route()
    if case == "no_capacity":
        capacity = None
    elif case == "different_model":
        capacity = route("vendor/report-model")
    elif case == "no_state":
        context.run_state = None
    else:
        context.run_state = SimpleNamespace()
    assert loop._context_tracked_agent(agent, context, capacity) is agent


async def test_loop_tracks_only_main_conversation_not_compaction_or_secondary_calls(tmp_path, monkeypatch):
    async def handle(*args, **kwargs):
        text = args[1] if args else kwargs["input"]
        return response({"main": 501, "summary": 20, "secondary": 30}[text])

    inner = MockModel(handler=handle)
    original = Agent(name="Root", model=inner)
    state = State()
    services = EngineServices(model_capacity=route(), model_for=lambda _name: inner)
    context = EngineContext(agent_name="Root", services=services, run_state=state)
    events = EventWriter(tmp_path)
    coordinator = AgentCoordinator(tmp_path, events, limits=CoordinationLimits())
    await coordinator.register("root", "Root", parent_id=None, task="local fixture")

    async def compact(agent, *args, **kwargs):
        assert agent is original
        await agent.model.get_response(**request("summary"))
        assert not state.updates
        return False

    class Stream:
        raw_responses = []

        def __init__(self, agent):
            self.agent = agent

        async def stream_events(self):
            await self.agent.model.get_response(**request("main"))
            before = list(state.updates)
            await services.model_for("dedupe").get_response(**request("secondary"))
            assert state.updates == before
            context.lifecycle_completion = LifecycleCompletion("finish_scan", {
                "success": True, "scan_completed": True,
            })
            for event in ():
                yield event

    def run_streamed(agent, **kwargs):
        assert agent is not original
        assert isinstance(agent.model, ContextUsageModel)
        return Stream(agent)

    monkeypatch.setattr(loop, "_compact_session", compact)
    monkeypatch.setattr(loop.Runner, "run_streamed", run_streamed)
    result = await loop.run_agent_loop(
        original, context, initial_input="seed", events=events, coordinator=coordinator,
    )
    assert result["scan_completed"]
    assert original.model is inner
    assert [value["phase"] for _, value in state.updates] == ["request", "response"]
    assert state.updates[-1][1]["input_tokens"] == 501
