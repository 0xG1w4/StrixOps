"""End-to-end operator-hint interruption — the delivery path strix never had.

Scenario: root spawns a scanner and parks in wait_for_agents; the test drops
a platform-format hint file into STRIXOPS_OPERATOR_HINTS_DIR mid-run; the
poller delivers it with force-interrupt; the woken root echoes the hint
token in a chat.message (which is exactly what the platform's token-echo
detection consumes to mark the hint delivered).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HINT_TOKEN = "echo12345"


def _events(run_dir: Path) -> list[dict]:
    events_file = run_dir / "events.jsonl"
    return [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_operator_hint_interrupts_and_gets_echoed(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    hints_dir = tmp_path / "hints"
    hints_dir.mkdir()
    instruction = tmp_path / "cred.txt"
    instruction.write_text("# instruction\nscan the target.", encoding="utf-8")

    env = {
        "PATH": os.environ.get("PATH", ""),
        "STRIX_RUNS": str(runs_root),
        "STRIX_OPERATOR_HINTS_DIR": str(hints_dir),
        "STRIXOPS_DRY_RUN": "1",
        "STRIXOPS_DRY_RUN_HINTS": "1",
        "HOME": os.environ.get("HOME", ""),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "strixops.cli",
            "--dry-run",
            "-t",
            "https://hint-test.example.com",
            "--scan-type",
            "web",
            "--instruction-file",
            str(instruction),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        # wait for the scanner child to appear, then drop the hint
        run_dir = _wait_for_run_dir(runs_root)
        _wait_for_event(
            run_dir, lambda e: e["event_type"] == "agent.created" and e["actor"]["agent_name"] == "scanner"
        )
        hint_path = hints_dir / "1000_hint-1.json"
        hint_path.write_text(
            json.dumps(
                {
                    "message_id": "hint-1",
                    "task_id": "t1",
                    "phase": "1",
                    "target": "https://hint-test.example.com",
                    "agent_id": "",
                    "agent_name": "",
                    "message": "Prioritize the authentication flows.",
                    "created_at": "2026-01-01T00:00:00Z",
                    "status": "queued",
                    "hint_token": HINT_TOKEN,
                }
            ),
            encoding="utf-8",
        )

        stdout, _ = proc.communicate(timeout=180)
        assert proc.returncode == 0, stdout

        events = _events(run_dir)
        # the echo: root's post-wake message contains the hint token
        echoes = [
            e
            for e in events
            if e["event_type"] == "chat.message" and f"[hint:{HINT_TOKEN}]" in e["payload"]["content"]
        ]
        assert echoes, "no token echo in chat messages"
        assert echoes[0]["actor"]["agent_name"] == "root agent"
        # the run still completes cleanly after the interruption
        completed = [e for e in events if e["event_type"] == "run.completed"]
        assert len(completed) == 1
    finally:
        if proc.poll() is None:
            proc.kill()


def _wait_for_run_dir(runs_root: Path, timeout: float = 60.0) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dirs = [p for p in runs_root.iterdir() if p.is_dir()]
        if dirs:
            return dirs[0]
        time.sleep(0.2)
    raise AssertionError("run dir never appeared")


def _wait_for_event(run_dir: Path, predicate, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in _events(run_dir):
            if predicate(event):
                return event
        time.sleep(0.2)
    raise AssertionError("expected event never appeared")
