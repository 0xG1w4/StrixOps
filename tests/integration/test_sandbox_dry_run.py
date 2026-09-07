"""Real-sandbox dry run (opt-in): scripted model + live Docker container.

Skipped unless ``STRIXOPS_SANDBOX_TEST=1`` — it needs a Docker daemon and
the sandbox image. Exercises the full live stack minus the LLM: container
bring-up, the Shell capability's ``exec_command`` inside the container, and
clean teardown.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from strixops.runtime.sandbox import default_image

pytestmark = pytest.mark.skipif(
    (os.environ.get("STRIXOPS_SANDBOX_TEST") or "").strip() not in {"1", "true", "yes"},
    reason="set STRIXOPS_SANDBOX_TEST=1 (requires Docker + sandbox image)",
)


@pytest.fixture(scope="module")
def sandbox_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[dict]]:
    runs_root = tmp_path_factory.mktemp("runs")
    instruction = tmp_path_factory.mktemp("misc") / "instruction.md"
    instruction.write_text("# sandbox\nverify exec.", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "strixops.cli",
            "--dry-run",
            "-t",
            "https://sandbox-test.example.com",
            "--scan-type",
            "web",
            "--instruction-file",
            str(instruction),
        ],
        env={
            "PATH": os.environ.get("PATH", ""),
            "STRIX_RUNS": str(runs_root),
            "STRIXOPS_DRY_RUN": "1",
            "STRIXOPS_DRY_RUN_SANDBOX": "1",
            "STRIXOPS_IMAGE": default_image(),
            "HOME": os.environ.get("HOME", ""),
        },
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    events_file = next(runs_root.rglob("events.jsonl"))
    events = [
        json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return events_file.parent, events


def test_exec_command_ran_in_container(sandbox_run):
    _, events = sandbox_run
    started = [
        e for e in events if e["event_type"] == "tool.execution.started" and "cmd" in e["payload"]["args"]
    ]
    assert started, "no exec_command call in events"
    assert any("strixops-sandbox-ok" in e["payload"]["args"]["cmd"] for e in started)


def test_exec_output_visible(sandbox_run):
    _, events = sandbox_run
    outputs = [
        e
        for e in events
        if e["event_type"] == "tool.execution.updated"
        and "strixops-sandbox-ok" in str(e["payload"]["result"])
    ]
    assert outputs, "container output not visible in tool result event"


def test_run_completes_with_sandbox(sandbox_run):
    _, events = sandbox_run
    completed = next(e for e in events if e["event_type"] == "run.completed")
    assert completed["payload"]["vulnerability_count"] == 1


@pytest.mark.parametrize("scan_type", ["web", "internal"])
def test_shared_image_tools_and_host_workspace_bind(tmp_path, scan_type):
    """Both modes get the complete toolset and persistent workspace mount."""
    import asyncio

    from strixops.runtime.sandbox import create_sandbox_session

    host_ws = tmp_path / "workspace"

    async def scenario() -> None:
        bundle = await create_sandbox_session(scan_type=scan_type, host_workspace_dir=str(host_ws))
        try:
            assert bundle.session.state.image == default_image()
            tools = await bundle.session.exec(
                "for tool in curl nmap agent-browser smbclient ldapsearch proxychains4 "
                "fscan chisel gs-netcat mongosh secretsdump.py certipy bloodhound-ce-python; "
                'do command -v "$tool" || exit 1; done',
                timeout=60,
            )
            assert tools.exit_code == 0, (tools.stdout, tools.stderr)
            result = await bundle.session.exec("echo bind-proof > /workspace/bind.txt", timeout=60)
            assert result.exit_code == 0, result.stderr
        finally:
            await bundle.teardown()

    asyncio.run(scenario())
    assert (host_ws / "bind.txt").read_text(encoding="utf-8").strip() == "bind-proof"
