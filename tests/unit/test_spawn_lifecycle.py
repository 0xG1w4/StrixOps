"""A synchronous spawn reply must already participate in lifecycle gates."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agents.tool_context import ToolContext

from strixops.engine import spawn
from strixops.engine.coordinator import STATUS_CRASHED, STATUS_STOPPED, AgentCoordinator
from strixops.engine.runner import _reap_children
from strixops.engine.scanconfig import EngineContext, EngineServices, ScanSpec
from strixops.platform.events import EventWriter
from strixops.tools.lifecycle import finish_scan


@pytest.fixture()
async def environment(tmp_path, monkeypatch):
    events = EventWriter(tmp_path)
    coordinator = AgentCoordinator(tmp_path, events)
    await coordinator.register("root", "root", parent_id=None, task="Local fixture")
    state = Mock(run_dir=tmp_path)
    services = EngineServices(
        coordinator=coordinator, events=events, run_state=state,
        spec=ScanSpec("https://fixture.invalid"),
    )
    root = EngineContext(run_state=state, services=services)
    monkeypatch.setattr(spawn, "build_child_agent", Mock(return_value=SimpleNamespace(name="child")))
    child_loop = AsyncMock()
    monkeypatch.setattr(spawn, "run_agent_loop", child_loop)
    return SimpleNamespace(
        coordinator=coordinator, events=events, state=state, root=root,
        spawn=spawn.make_spawn_child(services), child_loop=child_loop,
    )


def _spawn(environment):
    result = environment.spawn(
        name="child", task="Local fixture", parent_history=[], skills=[], parent=environment.root,
    )
    assert result["ok"]
    return result["agent_id"]


def _events(environment):
    return [json.loads(line) for line in environment.events.path.read_text().splitlines()]


async def test_spawn_is_visible_before_child_coroutine_runs_and_blocks_finish(environment):
    env = environment
    release = asyncio.Event()

    async def wait_for_release(*args, **kwargs):
        await release.wait()

    env.child_loop.side_effect = wait_for_release
    child_id = _spawn(env)
    try:
        # No yield since spawn: this is the race window in the original code.
        env.child_loop.assert_not_awaited()
        assert env.coordinator.active_ids(exclude="root") == [child_id]
        assert env.coordinator.children_of("root") == [child_id]
        assert child_id in env.coordinator.agent_ids()
        raw = json.dumps({
            "executive_summary": "Fixture", "methodology": "Fixture",
            "technical_analysis": "Fixture", "recommendations": "Fixture",
        })
        payload = json.loads(await finish_scan.on_invoke_tool(
            ToolContext(context=env.root, tool_name="finish_scan", tool_call_id="finish", tool_arguments=raw),
            raw,
        ))
        assert payload["success"] is False and "Child agents are still working" in payload["message"]
        assert env.root.lifecycle_completion is None
        env.state.update_final_fields.assert_not_called()
    finally:
        await _reap_children(env.coordinator)


async def test_teardown_cancels_a_child_before_startup_and_preserves_event_contract(environment):
    env = environment
    child_id = _spawn(env)
    child_task = env.coordinator._tasks[child_id]
    await _reap_children(env.coordinator)
    env.child_loop.assert_not_awaited()
    assert child_task.cancelled()
    assert env.coordinator.entry_of(child_id)["status"] == STATUS_STOPPED
    assert not env.coordinator.active_ids(exclude="root")
    child_events = [event for event in _events(env) if event["actor"]["agent_id"] == child_id]
    assert [event["event_type"] for event in child_events] == ["agent.created", "agent.status.updated"]
    assert [event["payload"]["status"] for event in child_events] == ["running", "stopped"]
    snapshot = json.loads((env.coordinator.run_dir / ".state" / "agents.json").read_text())
    assert snapshot[child_id]["status"] == STATUS_STOPPED


async def test_startup_snapshot_failure_leaves_a_terminal_child_without_unhandled_task_error(
    environment, monkeypatch,
):
    env = environment
    monkeypatch.setattr(
        env.coordinator, "snapshot", AsyncMock(side_effect=OSError("Fixture storage failure"))
    )
    child_id = _spawn(env)
    child_task = env.coordinator._tasks[child_id]
    await child_task
    env.child_loop.assert_not_awaited()
    assert child_task.done() and child_task.exception() is None
    assert env.coordinator.entry_of(child_id)["status"] == STATUS_CRASHED
    assert not env.coordinator.active_ids(exclude="root")
    await _reap_children(env.coordinator)
    child_events = [event for event in _events(env) if event["actor"]["agent_id"] == child_id]
    assert sum(event["event_type"] == "agent.created" for event in child_events) == 1
    assert any(event["payload"].get("status") == STATUS_CRASHED for event in child_events)
