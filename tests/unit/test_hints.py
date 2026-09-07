"""Operator-hints poller unit tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strixops.engine.coordinator import AgentCoordinator
from strixops.platform.events import EventWriter
from strixops.platform.hints import OperatorHintsPoller


def _write_hint(hints_dir: Path, name: str, **fields) -> Path:
    path = hints_dir / name
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


@pytest.fixture()
def setup(tmp_path: Path):
    events = EventWriter(tmp_path)
    coordinator = AgentCoordinator(tmp_path, events)
    hints_dir = tmp_path / "hints"
    hints_dir.mkdir()
    poller = OperatorHintsPoller(hints_dir, coordinator)
    return poller, coordinator, hints_dir


async def test_delivers_hint_to_root(setup):
    poller, coordinator, hints_dir = setup
    await coordinator.register("root", "root agent", parent_id=None, task="t")
    _write_hint(
        hints_dir,
        "1000_msg-1.json",
        message_id="msg-1",
        agent_id="",
        phase="1",
        message="focus on auth",
        hint_token="abc12345",
        created_at="2026-01-01T00:00:00Z",
    )
    await poller._poll_once()
    pending = coordinator.pending_messages("root")
    assert len(pending) == 1
    assert pending[0]["type"] == "operator_hint"
    assert "[Operator hint | token=abc12345 | phase=1]" in pending[0]["content"]
    assert "focus on auth" in pending[0]["content"]


async def test_routes_to_named_agent(setup):
    poller, coordinator, hints_dir = setup
    await coordinator.register("root", "root agent", parent_id=None, task="t")
    await coordinator.register("abc123", "scanner", parent_id="root", task="scan")
    _write_hint(
        hints_dir,
        "1000_msg-2.json",
        message_id="msg-2",
        agent_id="abc123",
        message="check smb",
        hint_token="ffff0001",
        created_at="2026-01-01T00:00:01Z",
    )
    await poller._poll_once()
    assert coordinator.pending_messages("abc123")
    assert not coordinator.pending_messages("root")


async def test_unknown_agent_falls_back_to_root(setup):
    poller, coordinator, hints_dir = setup
    await coordinator.register("root", "root agent", parent_id=None, task="t")
    _write_hint(
        hints_dir,
        "1000_msg-3.json",
        message_id="msg-3",
        agent_id="ghost",
        message="hi",
        hint_token="ffff0002",
        created_at="2026-01-01T00:00:02Z",
    )
    await poller._poll_once()
    assert coordinator.pending_messages("root")


async def test_skips_tmp_and_dedupes(setup):
    poller, coordinator, hints_dir = setup
    await coordinator.register("root", "root agent", parent_id=None, task="t")
    _write_hint(hints_dir, "1000_msg-4.json.tmp", message_id="msg-4", message="partial write")
    _write_hint(
        hints_dir,
        "1001_msg-5.json",
        message_id="msg-5",
        message="one",
        hint_token="aaaa0001",
        created_at="2026-01-01T00:00:03Z",
    )
    await poller._poll_once()
    await poller._poll_once()  # second poll: same file — must not redeliver
    pending = coordinator.pending_messages("root")
    assert len(pending) == 1
    assert pending[0]["content"].endswith("one")


async def test_orders_by_created_at(setup):
    poller, coordinator, hints_dir = setup
    await coordinator.register("root", "root agent", parent_id=None, task="t")
    _write_hint(
        hints_dir,
        "1002_b.json",
        message_id="m-b",
        message="second",
        hint_token="t0000002",
        created_at="2026-01-01T00:00:05Z",
    )
    _write_hint(
        hints_dir,
        "1001_a.json",
        message_id="m-a",
        message="first",
        hint_token="t0000001",
        created_at="2026-01-01T00:00:04Z",
    )
    await poller._poll_once()
    contents = [m["content"] for m in coordinator.pending_messages("root")]
    assert contents[0].endswith("first")
    assert contents[1].endswith("second")


async def test_corrupt_json_ignored(setup):
    poller, coordinator, hints_dir = setup
    await coordinator.register("root", "root agent", parent_id=None, task="t")
    (hints_dir / "9999_bad.json").write_text("{not json", encoding="utf-8")
    await poller._poll_once()
    assert not coordinator.pending_messages("root")
