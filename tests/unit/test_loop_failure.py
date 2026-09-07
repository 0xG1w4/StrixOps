"""Agent failures retain the actual attempt count and cause across event surfaces."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agents.tool_context import ToolContext
from agents.exceptions import ModelBehaviorError

from strixops.engine import loop
from strixops.engine.coordinator import STATUS_CRASHED, STATUS_RUNNING, AgentCoordinator
from strixops.engine.scanconfig import EngineContext, LifecycleCompletion
from strixops.platform.events import EventWriter
from strixops.tools.lifecycle import agent_finish


class _Stream:
    def __init__(self, *, output="plain text without lifecycle", error=None, finish_context=None):
        self.final_output = output
        self.error = error
        self.finish_context = finish_context

    async def stream_events(self):
        if self.error is not None:
            raise self.error
        if self.finish_context is not None:
            self.final_output = await agent_finish.on_invoke_tool(
                ToolContext(
                    context=self.finish_context, tool_name="agent_finish",
                    tool_call_id="finish", tool_arguments="{}",
                ),
                json.dumps({"result_summary": "Fixture complete", "report_to_parent": False}),
            )
        for event in ():
            yield event

    def to_input_list(self):
        return [{"role": "assistant", "content": str(self.final_output)}]


@pytest.fixture()
async def agent_env(tmp_path: Path):
    events = EventWriter(tmp_path)
    coordinator = AgentCoordinator(tmp_path, events)
    await coordinator.register("root", "root", parent_id=None, task="root task")
    await coordinator.register("child", "scanner", parent_id="root", task="child task")
    context = EngineContext(agent_id="child", agent_name="scanner", parent_id="root")
    return SimpleNamespace(context=context, events=events, coordinator=coordinator)


async def _run(env, context=None):
    return await loop.run_agent_loop(
        SimpleNamespace(name="fake agent"),
        context or env.context,
        initial_input="start",
        events=env.events,
        coordinator=env.coordinator,
    )


def _events(env):
    return [json.loads(line) for line in env.events.path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("during_stream", [False, True])
async def test_first_model_error_reports_one_attempt_and_exact_cause(
    agent_env,
    monkeypatch,
    capsys,
    during_stream,
):
    env = agent_env
    error = ModelBehaviorError("Unknown tool: apply_patch; call_id=call_bad")
    streamed = Mock(return_value=_Stream(error=error)) if during_stream else Mock(side_effect=error)
    monkeypatch.setattr(loop.Runner, "run_streamed", streamed)
    print_spy = Mock(wraps=print)
    monkeypatch.setattr(loop, "print", print_spy, raising=False)
    expected = (
        "Agent scanner (child) ended without a lifecycle tool after 1 attempt (0 recovery nudges); "
        f"last error: ModelBehaviorError: {error}"
    )

    # Status/event consumers must already be able to obtain the stored cause.
    set_status = env.coordinator.set_status

    async def check_status(agent_id, status):
        if status == STATUS_CRASHED:
            assert env.context.failure_reason == expected
        await set_status(agent_id, status)

    monkeypatch.setattr(env.coordinator, "set_status", check_status)
    assert await _run(env) is None
    streamed.assert_called_once()
    assert env.context.failure_reason == expected
    assert env.coordinator.entry_of("child")["status"] == STATUS_CRASHED
    assert env.coordinator.pending_messages("root") == [
        {
            "from": "child",
            "from_name": "scanner",
            "type": "agent_crashed",
            "priority": "high",
            "content": expected,
        }
    ]
    crashes = [event for event in _events(env) if event["event_type"] == "chat.message"]
    assert [event["payload"]["content"] for event in crashes] == [f"[agent crashed] {expected}"]
    print_spy.assert_called_once_with(f"[agent crashed] {expected}", flush=True)
    assert capsys.readouterr().out == f"[agent crashed] {expected}\n"


async def test_lifecycle_nudge_exhaustion_counts_six_executions_and_five_nudges(agent_env, monkeypatch):
    streamed = Mock(side_effect=lambda *args, **kwargs: _Stream())
    monkeypatch.setattr(loop.Runner, "run_streamed", streamed)
    assert await _run(agent_env) is None

    assert streamed.call_count == 6
    assert "after 6 attempts (5 recovery nudges)" in agent_env.context.failure_reason
    assert "last error:" not in agent_env.context.failure_reason
    assert streamed.call_args_list[0].kwargs["input"] == []
    assert streamed.call_args_list[0].kwargs["session"] is not None
    for nudge_number, call in enumerate(streamed.call_args_list[1:], start=1):
        assert len(call.kwargs["input"]) == 1
        assert call.kwargs["input"][0]["role"] == "user"
        assert f"(recovery attempt {nudge_number}/5)" in call.kwargs["input"][0]["content"]
    notices = agent_env.coordinator.pending_messages("root")
    assert len(notices) == 1 and notices[0]["content"] == agent_env.context.failure_reason


@pytest.mark.parametrize("output", [
    {"success": True, "agent_finished": True},
    '{"success": true, "agent_finished": true}',
    '{"success": true, "scan_completed": true}',
    '{"agent_finished": "true", "scan_completed": 1}',
])
async def test_model_completion_payload_never_authorizes_retirement(agent_env, monkeypatch, output):
    streamed = Mock(side_effect=lambda *args, **kwargs: _Stream(output=output))
    monkeypatch.setattr(loop.Runner, "run_streamed", streamed)
    assert await _run(agent_env) is None
    assert streamed.call_count == loop.MAX_NUDGES + 1
    assert agent_env.context.lifecycle_completion is None


async def test_completion_from_a_previous_execution_is_reset(agent_env, monkeypatch):
    agent_env.context.lifecycle_completion = LifecycleCompletion(
        "agent_finish", {"success": True, "agent_finished": True}
    )
    monkeypatch.setattr(loop.Runner, "run_streamed", Mock(return_value=_Stream()))
    assert await _run(agent_env) is None
    assert agent_env.context.lifecycle_completion is None


async def test_wrong_role_completion_record_is_not_accepted(agent_env, monkeypatch):
    def streamed(*args, **kwargs):
        # A trusted fixture exercises the loop's independent role check.
        kwargs["context"].lifecycle_completion = LifecycleCompletion(
            "finish_scan", {"success": True, "scan_completed": True}
        )
        return _Stream(output={"success": True, "scan_completed": True})

    monkeypatch.setattr(loop.Runner, "run_streamed", streamed)
    assert await _run(agent_env) is None


async def test_successful_tool_completion_does_not_depend_on_final_output(agent_env, monkeypatch):
    class MisleadingOutputStream(_Stream):
        async def stream_events(self):
            async for event in super().stream_events():
                yield event
            self.final_output = "This text is not a completion payload"

    streamed = Mock(return_value=MisleadingOutputStream(finish_context=agent_env.context))
    monkeypatch.setattr(loop.Runner, "run_streamed", streamed)
    payload = await _run(agent_env)
    assert payload["success"] is True and payload["agent_finished"] is True
    streamed.assert_called_once()


@pytest.mark.parametrize("during_stream", [False, True])
async def test_cancellation_propagates_without_crash_or_stale_reason(
    agent_env,
    monkeypatch,
    capsys,
    during_stream,
):
    env = agent_env
    env.context.failure_reason = "previous execution failure"
    cancelled = asyncio.CancelledError()
    streamed = Mock(return_value=_Stream(error=cancelled)) if during_stream else Mock(side_effect=cancelled)
    monkeypatch.setattr(loop.Runner, "run_streamed", streamed)

    with pytest.raises(asyncio.CancelledError):
        await _run(env)

    assert env.context.failure_reason == ""
    assert env.coordinator.entry_of("child")["status"] == STATUS_RUNNING
    assert not env.coordinator.pending_messages("root")
    assert not any(event["event_type"] == "chat.message" for event in _events(env))
    assert "[agent crashed]" not in capsys.readouterr().out
    streamed.assert_called_once()


async def test_failure_reason_is_per_context_and_resets_when_context_is_reused(agent_env, monkeypatch):
    env = agent_env
    sibling = EngineContext(agent_id="sibling", agent_name="other scanner", parent_id="root")
    await env.coordinator.register("sibling", "other scanner", parent_id="root", task="separate task")
    streamed = Mock(
        side_effect=[
            ModelBehaviorError("first agent failed"),
            _Stream(finish_context=sibling),
            _Stream(finish_context=env.context),
        ]
    )
    monkeypatch.setattr(loop.Runner, "run_streamed", streamed)

    assert env.context.failure_reason == sibling.failure_reason == ""
    assert await _run(env) is None
    reason = env.context.failure_reason
    assert "first agent failed" in reason and sibling.failure_reason == ""
    finished = await _run(env, sibling)
    assert finished["agent_finished"] is True
    assert env.context.failure_reason == reason and sibling.failure_reason == ""
    assert await _run(env) == finished
    assert env.context.failure_reason == ""
