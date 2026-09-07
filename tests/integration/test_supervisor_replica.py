"""Integration: a byte-faithful replica of the platform supervisor's spawn
contract binds our run dir, drives the status machine, and collects the
exit code.

Mirrors ``apps/api/services/supervisor.py``: same argv, same env keys,
``start_new_session=True``, piped stdio, cwd=$STRIX_HOME, 60s run-dir
detection window, first-event → running, ``run.completed`` → reporting.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

RUN_DIR_TIMEOUT = 60.0
_SUFFIX_RE = re.compile(r"^.+_[0-9a-fA-F]{4}$")


def _spawn(runs_root: Path, instruction: Path) -> subprocess.Popen:
    argv = [
        sys.executable,
        "-m",
        "strixops.cli",
        "-t",
        "https://supervisor-replica.example.com",
        "--scan-type",
        "web",
        "--crypto",
        "--instruction-file",
        str(instruction),
        "--dry-run",
    ]
    env = os.environ.copy()
    env.update(
        {
            "STRIX_RUNS": str(runs_root),
            "STRIX_OPERATOR_HINTS_DIR": str(runs_root / "hints"),
            "STRIX_HOME": str(runs_root / "home"),
            "STRIXOPS_DRY_RUN": "1",
        }
    )
    (runs_root / "home").mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        argv,
        cwd=str(runs_root / "home"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _wait_run_dir(runs_root: Path, before: set[str]) -> Path:
    deadline = time.monotonic() + RUN_DIR_TIMEOUT
    while time.monotonic() < deadline:
        for path in runs_root.iterdir():
            if path.is_dir() and _SUFFIX_RE.match(path.name) and path.name not in before:
                return path
        time.sleep(0.2)
    raise AssertionError("no run dir appeared within the supervisor detection window")


def _tail_event(run_dir: Path, event_type: str, deadline_s: float = 30.0) -> dict:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        events_file = run_dir / "events.jsonl"
        if events_file.exists():
            for line in events_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("event_type") == event_type:
                    return event
        time.sleep(0.2)
    raise AssertionError(f"event {event_type} never appeared")


def test_supervisor_contract(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    instruction = tmp_path / "cred.txt"
    instruction.write_text("# instruction\ntest the target.", encoding="utf-8")

    before = {p.name for p in runs_root.iterdir()}
    proc = _spawn(runs_root, instruction)
    try:
        run_dir = _wait_run_dir(runs_root, before)
        assert (run_dir / "events.jsonl").exists(), "events.jsonl missing inside detection window"

        first = _tail_event(run_dir, "run.configured")  # first event → platform flips preparing→running
        assert first["payload"]["scan_config"]["target"] == "https://supervisor-replica.example.com"

        completed = _tail_event(run_dir, "run.completed")
        assert completed["payload"]["vulnerability_count"] >= 1

        stdout, _ = proc.communicate(timeout=60)
        assert proc.returncode == 0, stdout  # exit 0 + events → platform goes to reporting

        artifacts_present = [
            "penetration_test_report.md",
            "vulnerabilities.csv",
            "vulnerabilities.json",
            "run.json",
        ]
        for name in artifacts_present:
            assert (run_dir / name).is_file(), f"missing artifact {name}"
        run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert run_record["status"] == "completed"
    finally:
        if proc.poll() is None:
            proc.kill()
