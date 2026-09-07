"""Run evidence survives completion, failure, and cancellation without Docker."""

from __future__ import annotations

import asyncio
import json
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agents import RunContextWrapper
from agents.tool_context import ToolContext

from strixops.config import provider
from strixops.config.settings import EngineSettings
from strixops.engine import runner
from strixops.engine.scanconfig import ScanSpec
from strixops.runtime import sandbox as sandbox_module
from strixops.report.state import RunState
from strixops.tools.lifecycle import finish_scan


@pytest.mark.parametrize("configured_workspace", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "cancel-during-copy"])
async def test_run_collects_evidence_before_report_and_teardown(
    tmp_path, monkeypatch, configured_workspace, outcome
):
    run_dir = tmp_path / "runs" / "evidence-fixture_a001"
    configured = tmp_path / "configured-workspace"
    settings = EngineSettings(
        llm_api_base="http://127.0.0.1:1/v1",
        llm_api_key="fixture-key",
        strix_llm="fixture-model",
        strix_runs=str(tmp_path / "runs"),
        operator_hints_dir="",
        host_workspace_dir=str(configured) if configured_workspace else "",
        dry_run=False,
    )
    workspaces = []
    snapshots = []
    collect_started = asyncio.Event()
    release_collection = threading.Event()
    if outcome == "cancel-during-copy":
        original_collect = RunState.collect_evidence
        event_loop = asyncio.get_running_loop()

        def delayed_collect(state, workspace):
            event_loop.call_soon_threadsafe(collect_started.set)
            assert release_collection.wait(timeout=5)
            return original_collect(state, workspace)

        monkeypatch.setattr(RunState, "collect_evidence", delayed_collect)

    async def create_sandbox(**kwargs):
        workspace = Path(kwargs["host_workspace_dir"])
        assert workspace == (configured if configured_workspace else run_dir / "workspace")
        # The sandbox never receives the parent containing trusted run state.
        assert workspace != run_dir
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
        (workspace / "output").mkdir()
        (workspace / "output" / "result.txt").write_text("local synthetic evidence")
        workspaces.append(workspace)

        async def teardown(**kwargs):
            snapshots.append(json.loads((run_dir / "run.json").read_text()))
            assert (run_dir / "evidence" / "result.txt").read_text() == "local synthetic evidence"
            assert (workspace / "output" / "result.txt").exists()
            if outcome == "success":
                report = (run_dir / "penetration_test_report.md").read_text()
                assert "deliverable: 1" in report

        return SimpleNamespace(session=object(), teardown=AsyncMock(side_effect=teardown))

    async def fake_loop(agent, context, **kwargs):
        if outcome == "failure":
            raise RuntimeError("synthetic model failure")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        arguments = json.dumps(
            {
                "executive_summary": "Fixture summary",
                "methodology": "Synthetic only",
                "technical_analysis": "Fixture details",
                "recommendations": "Review fixture",
            }
        )
        tool_context = ToolContext.from_agent_context(
            RunContextWrapper(context),
            "finish",
            tool_name="finish_scan",
            tool_arguments=arguments,
        )
        return json.loads(await finish_scan.on_invoke_tool(tool_context, arguments))

    monkeypatch.setattr(sandbox_module, "create_sandbox_session", create_sandbox)
    monkeypatch.setattr(provider, "make_platform_model", Mock(return_value=SimpleNamespace(model="fixture")))
    monkeypatch.setattr(runner, "build_root_agent", Mock(return_value=SimpleNamespace(name="fixture")))
    monkeypatch.setattr(runner, "generate_run_name", lambda *args: run_dir.name)
    monkeypatch.setattr(runner, "run_agent_loop", fake_loop)
    spec = ScanSpec(target="fixture.invalid", scan_type="internal", report_language="en")
    if outcome == "cancel-during-copy":
        task = asyncio.create_task(runner.run_scan(spec, settings))
        try:
            await asyncio.wait_for(collect_started.wait(), timeout=5)
            task.cancel()
        finally:
            release_collection.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await runner.run_scan(spec, settings)
    else:
        assert await runner.run_scan(spec, settings) == (
            runner.EXIT_OK if outcome == "success" else runner.EXIT_FAILED
        )
    assert len(snapshots) == len(workspaces) == 1
    record = snapshots[0]
    assert record["status"] == ("completed" if outcome == "success" else "failed")
    assert record["evidence"]["count"] == record["evidence"]["persisted_count"] == 1
    assert record["evidence"]["status"] == "complete"
    assert record["workspace"]["persistent"] is True
