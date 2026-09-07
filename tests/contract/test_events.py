"""events.jsonl produced by a dry run must satisfy the platform's consumers:

* valid envelope with all four keys on every line
* only vocabulary the platform parses
* ``run.completed`` exactly once, ``agent.status.updated`` only on transitions
* tool-call events replay through the frozen classifier to the intended kinds
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .oracles import infer_unified_tool_context

KNOWN_EVENT_TYPES = {
    "run.configured",
    "agent.created",
    "agent.status.updated",
    "chat.message",
    "tool.execution.started",
    "tool.execution.updated",
    "vulnerability.found",
    "finding.internal_created",
    "run.completed",
    # engine extension: token accounting per turn (ignored by the platform
    # parser; consumed by the console)
    "usage.updated",
}


@pytest.fixture(scope="module")
def dry_run_events(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[dict]]:
    runs_root = tmp_path_factory.mktemp("runs")
    instruction = tmp_path_factory.mktemp("misc") / "instruction.md"
    instruction.write_text("# dry run\nverify contract.", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "strixops.cli",
            "--dry-run",
            "-t",
            "https://contract-test.example.com",
            "--scan-type",
            "web",
            "--crypto",
            "--instruction-file",
            str(instruction),
        ],
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "STRIX_RUNS": str(runs_root),
            "STRIXOPS_DRY_RUN": "1",
            "HOME": __import__("os").environ.get("HOME", ""),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    events_file = next(runs_root.rglob("events.jsonl"))
    events = [
        json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return events_file.parent, events


def test_every_line_has_valid_envelope(dry_run_events) -> None:
    _, events = dry_run_events
    assert events
    for event in events:
        assert set(event.keys()) >= {"timestamp", "event_type", "actor", "payload"}
        assert isinstance(event["actor"], dict)
        assert set(event["actor"].keys()) == {"agent_id", "agent_name"}
        assert event["event_type"] in KNOWN_EVENT_TYPES


def test_event_ordering_and_uniqueness(dry_run_events) -> None:
    _, events = dry_run_events
    types = [e["event_type"] for e in events]
    assert types[0] == "run.configured"
    assert types.count("run.configured") == 1
    assert types.count("run.completed") == 1
    assert types[-1] in {"run.completed", "agent.status.updated"}
    completed_at = types.index("run.completed")
    assert "vulnerability.found" in types[:completed_at]


def test_status_updated_only_on_transitions(dry_run_events) -> None:
    _, events = dry_run_events
    agent_status: dict[str, str] = {}
    for event in events:
        if event["event_type"] == "agent.created":
            agent_status[event["actor"]["agent_id"]] = event["payload"]["status"]
        elif event["event_type"] == "agent.status.updated":
            agent_id = event["actor"]["agent_id"]
            assert agent_id in agent_status, "status update for unregistered agent"
            assert agent_status[agent_id] != event["payload"]["status"], "repeat status event"
            agent_status[agent_id] = event["payload"]["status"]


def test_tool_events_pair_and_classify(dry_run_events) -> None:
    _, events = dry_run_events
    started = [e for e in events if e["event_type"] == "tool.execution.started"]
    updated = [e for e in events if e["event_type"] == "tool.execution.updated"]
    assert started, "no tool events in dry run"
    assert len(updated) >= len(started) - 1  # finish tool result may stop the run before its output event

    kinds = [infer_unified_tool_context(e["payload"]["args"]) for e in started]
    kind_names = [k.get("kind") or "thinking" for k in kinds]
    assert "todo" in kind_names
    assert "vulnerability_report" in kind_names
    assert "finish" in kind_names


def test_run_completed_payload(dry_run_events) -> None:
    _, events = dry_run_events
    completed = next(e for e in events if e["event_type"] == "run.completed")
    payload = completed["payload"]
    assert isinstance(payload["duration_seconds"], int)
    assert payload["vulnerability_count"] >= 1
