"""Multi-agent dry run: spawn → work → completion report → finish.

Asserts the platform-observable tree: child ``agent.created`` with
``parent_id``, per-actor chat/tool events, the child's ``vulnerability.found``
, terminal statuses in order, ``agents.json`` lineage, and a clean exit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def multi_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[dict], dict]:
    runs_root = tmp_path_factory.mktemp("runs")
    instruction = tmp_path_factory.mktemp("misc") / "instruction.md"
    instruction.write_text("# multi\nverify delegation.", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "strixops.cli",
            "--dry-run",
            "-t",
            "https://multi-agent.example.com",
            "--scan-type",
            "web",
            "--instruction-file",
            str(instruction),
        ],
        env={
            "PATH": os.environ.get("PATH", ""),
            "STRIX_RUNS": str(runs_root),
            "STRIXOPS_DRY_RUN": "1",
            "HOME": os.environ.get("HOME", ""),
        },
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    events_file = next(runs_root.rglob("events.jsonl"))
    events = [
        json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    agents = json.loads((events_file.parent / ".state" / "agents.json").read_text(encoding="utf-8"))
    return events_file.parent, events, agents


def test_child_created_with_parent(multi_run):
    _, events, agents = multi_run
    created = {e["actor"]["agent_name"]: e for e in events if e["event_type"] == "agent.created"}
    assert set(created) >= {"root agent", "scanner"}
    assert created["root agent"]["payload"]["parent_id"] is None
    assert created["scanner"]["payload"]["parent_id"] == "root"
    assert created["scanner"]["payload"]["status"] == "running"
    # agents.json is keyed by agent id — find the scanner entry by name
    scanner_entry = next(entry for entry in agents.values() if entry["name"] == "scanner")
    assert scanner_entry["parent_id"] == "root"


def test_child_files_the_finding(multi_run):
    _, events, _ = multi_run
    found = [e for e in events if e["event_type"] == "vulnerability.found"]
    assert len(found) == 1
    assert found[0]["actor"]["agent_name"] == "scanner"


def test_child_actor_events_flow(multi_run):
    _, events, _ = multi_run
    scanner_tools = [
        e
        for e in events
        if e["event_type"] == "tool.execution.started" and e["actor"]["agent_name"] == "scanner"
    ]
    tool_names = {tuple(sorted(e["payload"]["args"])) for e in scanner_tools}
    # scanner's script: think → create_vulnerability_report → agent_finish
    assert any("result_summary" in keys for keys in tool_names)


def test_status_sequence(multi_run):
    _, events, _ = multi_run
    transitions = [
        (e["actor"]["agent_name"], e["payload"]["status"])
        for e in events
        if e["event_type"] == "agent.status.updated"
    ]
    assert ("scanner", "completed") in transitions
    assert ("root agent", "waiting") in transitions
    assert transitions[-1] == ("root agent", "completed")
    # transitions-only: no (agent, status) pair repeats consecutively for same agent
    per_agent: dict[str, str] = {}
    for name, status in transitions:
        assert per_agent.get(name) != status, f"repeat transition {name} → {status}"
        per_agent[name] = status


def test_root_received_completion_report(multi_run):
    _, events, _ = multi_run
    wait_results = [
        e["payload"]["result"]
        for e in events
        if e["event_type"] == "tool.execution.updated"
        and isinstance(e["payload"]["result"], dict)
        and "messages" in e["payload"]["result"]
    ]
    assert wait_results, "wait_for_agents result event missing"
    messages = wait_results[-1]["messages"]
    assert any("Completion report from scanner" in m for m in messages)


def test_run_completed_with_child_finding(multi_run):
    _, events, _ = multi_run
    completed = next(e for e in events if e["event_type"] == "run.completed")
    assert completed["payload"]["vulnerability_count"] == 1
    snapshot_agents = json.loads((multi_run[0] / ".state" / "agents.json").read_text(encoding="utf-8"))
    scanner_entry = next(entry for entry in snapshot_agents.values() if entry["name"] == "scanner")
    assert scanner_entry["status"] == "completed"
    assert any(entry["name"] == "root agent" for entry in snapshot_agents.values())
