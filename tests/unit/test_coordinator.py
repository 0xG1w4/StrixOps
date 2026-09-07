"""Coordinator unit tests: transitions-only events, mailboxes, lineage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strixops.engine.coordinator import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_WAITING,
    AgentCoordinator,
)
from strixops.platform.events import EventWriter


@pytest.fixture()
def setup(tmp_path: Path):
    events = EventWriter(tmp_path)
    coordinator = AgentCoordinator(tmp_path, events)
    return coordinator, events, tmp_path


async def read_events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_register_emits_created_with_running(setup):
    coordinator, _, tmp_path = setup
    await coordinator.register("root", "root agent", parent_id=None, task="t")
    events = await read_events(tmp_path)
    assert events[-1]["event_type"] == "agent.created"
    assert events[-1]["payload"]["status"] == STATUS_RUNNING
    assert events[-1]["payload"]["parent_id"] is None


async def test_status_changed_emits_only_on_transition(setup):
    coordinator, _, tmp_path = setup
    await coordinator.register("root", "root agent", parent_id=None, task="t")
    await coordinator.set_status("root", STATUS_WAITING)
    await coordinator.set_status("root", STATUS_WAITING)  # repeat — no event
    await coordinator.set_status("root", STATUS_RUNNING)  # back — event

    status_events = [e for e in await read_events(tmp_path) if e["event_type"] == "agent.status.updated"]
    assert [e["payload"]["status"] for e in status_events] == [STATUS_WAITING, STATUS_RUNNING]


async def test_children_and_active(setup):
    coordinator, _, _tmp = setup
    await coordinator.register("root", "root agent", parent_id=None, task="t")
    await coordinator.register("c1", "scanner", parent_id="root", task="scan")
    await coordinator.register("c2", "prober", parent_id="root", task="probe")

    assert coordinator.children_of("root") == ["c1", "c2"]
    assert sorted(coordinator.active_ids(exclude="root")) == ["c1", "c2"]

    await coordinator.set_status("c1", STATUS_COMPLETED)
    assert coordinator.active_ids(exclude="root") == ["c2"]
    assert coordinator.terminal_ids() == ["c1"]


async def test_mailbox_send_drain(setup):
    coordinator, _, _tmp = setup
    await coordinator.register("root", "root agent", parent_id=None, task="t")
    await coordinator.register("c1", "scanner", parent_id="root", task="scan")

    assert await coordinator.send("c1", {"from": "root", "content": "hello"})
    assert not await coordinator.send("ghost", {"from": "root", "content": "x"})

    assert [m["content"] for m in coordinator.pending_messages("c1")] == ["hello"]
    drained = coordinator.drain_messages("c1")
    assert [m["content"] for m in drained] == ["hello"]
    assert coordinator.pending_messages("c1") == []


async def test_snapshot_has_lineage(setup):
    coordinator, _, tmp_path = setup
    await coordinator.register("root", "root agent", parent_id=None, task="t")
    await coordinator.register("c1", "scanner", parent_id="root", task="scan")
    snapshot = json.loads((tmp_path / ".state" / "agents.json").read_text(encoding="utf-8"))
    assert snapshot["root"]["parent_id"] is None
    assert snapshot["c1"]["parent_id"] == "root"
    assert snapshot["c1"]["name"] == "scanner"
