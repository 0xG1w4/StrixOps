"""Scan orchestration persists model failures before diagnostic sandbox teardown."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agents import ModelResponse, ModelSettings, ModelTracing
from agents.tool_context import ToolContext
from agents.exceptions import ModelBehaviorError
from agents.items import Usage

from strixops.config import provider
from strixops.config.settings import EngineSettings
from strixops.engine import loop, runner, sessions
from strixops.engine.scanconfig import ScanSpec
from strixops.platform import artifacts
from strixops.report.state import RunState
from strixops.runtime import sandbox as sandbox_module
from strixops.tools import output_store
from strixops.tools.lifecycle import finish_scan


class _FinishedStream:
    def __init__(self, context):
        self.context = context
        self.final_output = None

    async def stream_events(self):
        self.final_output = await finish_scan.on_invoke_tool(
            ToolContext(
                context=self.context, tool_name="finish_scan",
                tool_call_id="finish", tool_arguments="{}",
            ),
            json.dumps({
                "executive_summary": "Fixture complete",
                "methodology": "Local fixture",
                "technical_analysis": "No external systems accessed",
                "recommendations": "None",
            }),
        )
        for event in ():
            yield event


@pytest.fixture()
def runtime(monkeypatch, tmp_path):
    run_dir = tmp_path / "runs" / "failure-fixture_a001"
    settings = EngineSettings(
        llm_api_base="http://127.0.0.1:1/v1",
        llm_api_key="fixture-key",
        strix_llm="fixture-model",
        strix_runs=str(tmp_path / "runs"),
        operator_hints_dir="",
        host_workspace_dir="",
        dry_run=False,
    )
    teardown_records = []

    async def teardown(**kwargs):
        teardown_records.append(json.loads((run_dir / "run.json").read_text(encoding="utf-8")))

    sandbox = SimpleNamespace(client=object(), session=object(), teardown=AsyncMock(side_effect=teardown))
    create_sandbox = AsyncMock(return_value=sandbox)
    make_model = Mock(return_value=SimpleNamespace(model="fixture-model"))
    monkeypatch.setattr(sandbox_module, "create_sandbox_session", create_sandbox)
    monkeypatch.setattr(provider, "make_platform_model", make_model)
    monkeypatch.setattr(runner, "build_root_agent", Mock(return_value=SimpleNamespace(name="fake root")))
    monkeypatch.setattr(runner, "generate_run_name", lambda *args: run_dir.name)
    return SimpleNamespace(
        settings=settings,
        spec=ScanSpec(target="https://fixture.invalid", scan_type="web"),
        run_dir=run_dir,
        sandbox=sandbox,
        teardown_records=teardown_records,
        create_sandbox=create_sandbox,
        make_model=make_model,
    )


def _record(runtime):
    return json.loads((runtime.run_dir / "run.json").read_text(encoding="utf-8"))


async def test_model_failure_is_persisted_logged_and_passed_to_diagnostic_teardown(
    runtime,
    monkeypatch,
    capsys,
):
    error = ModelBehaviorError("Tool arguments rejected for apply_patch: missing patch")
    streamed = Mock(side_effect=error)
    monkeypatch.setattr(loop.Runner, "run_streamed", streamed)

    assert await runner.run_scan(runtime.spec, runtime.settings) == runner.EXIT_FAILED
    streamed.assert_called_once()
    context = streamed.call_args.kwargs["context"]
    expected = (
        f"Agent {runner.ROOT_AGENT_NAME} (root) ended without a lifecycle tool "
        f"after 1 attempt (0 recovery nudges); last error: ModelBehaviorError: {error}"
    )
    record = _record(runtime)
    assert record["status"] == "failed"
    assert record["failure_reason"] == context.failure_reason == expected
    assert runtime.teardown_records[0]["failure_reason"] == expected
    runtime.sandbox.teardown.assert_awaited_once_with(diagnostics_dir=runtime.run_dir / "diagnostics")
    runtime.create_sandbox.assert_awaited_once()
    runtime.make_model.assert_called_once_with(runtime.settings)
    output = capsys.readouterr().out
    assert "Sandbox ready; starting agent…" in output
    assert f"[agent crashed] {expected}" in output
    assert f"Scan failed: {expected}" in output
    assert output.index("Sandbox ready") < output.index("[agent crashed]")


async def test_missing_specific_cause_keeps_generic_failure_fallback(runtime, monkeypatch, capsys):
    monkeypatch.setattr(runner, "run_agent_loop", AsyncMock(return_value=None))
    assert await runner.run_scan(runtime.spec, runtime.settings) == runner.EXIT_FAILED
    reason = "root agent ended without a successful finish_scan"
    assert _record(runtime)["failure_reason"] == reason
    assert f"Scan failed: {reason}" in capsys.readouterr().out
    runtime.sandbox.teardown.assert_awaited_once_with(diagnostics_dir=runtime.run_dir / "diagnostics")


async def test_successful_scan_tears_down_without_diagnostic_arguments(runtime, monkeypatch, capsys):
    streamed = Mock(side_effect=lambda *args, **kwargs: _FinishedStream(kwargs["context"]))
    monkeypatch.setattr(loop.Runner, "run_streamed", streamed)
    assert await runner.run_scan(runtime.spec, runtime.settings) == runner.EXIT_OK
    record = _record(runtime)
    assert record["status"] == "completed"
    assert not record.get("failure_reason")
    assert runtime.teardown_records[0]["status"] == "completed"
    runtime.sandbox.teardown.assert_awaited_once_with()
    assert "Scan failed:" not in capsys.readouterr().out


async def test_scan_cancellation_still_persists_interruption_and_collects_diagnostics(
    runtime,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(loop.Runner, "run_streamed", Mock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await runner.run_scan(runtime.spec, runtime.settings)
    assert _record(runtime)["status"] == "failed"
    assert _record(runtime)["failure_reason"] == "interrupted"
    runtime.sandbox.teardown.assert_awaited_once_with(diagnostics_dir=runtime.run_dir / "diagnostics")
    assert "[agent crashed]" not in capsys.readouterr().out


async def test_live_caido_run_records_its_actual_container_for_status_panel(runtime, monkeypatch):
    container_id = "ab" * 32
    runtime.sandbox.session = SimpleNamespace(state=SimpleNamespace(container_id=container_id))
    runtime.sandbox.caido = object()
    monkeypatch.setattr(
        loop.Runner, "run_streamed", Mock(side_effect=lambda *args, **kwargs: _FinishedStream(kwargs["context"]))
    )

    assert await runner.run_scan(runtime.spec, runtime.settings) == runner.EXIT_OK
    binding = json.loads((runtime.run_dir / ".state" / "caido-runtime.json").read_text())
    assert binding == {"container_id": container_id, "run_name": runtime.run_dir.name}


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_run_sessions_close_after_child_cleanup_and_workspace_spills_stop(
    runtime,
    monkeypatch,
    outcome,
):
    """The child's final DB write must complete before either session closes."""
    lifecycle = []
    opened = {}
    writes = {}
    preview = []
    full_output = "\n".join(f"local observation {index}: 中文 evidence" for index in range(60))
    database = runtime.run_dir / ".state" / "agents.db"
    original_open = runner.open_agent_session

    def open_session(agent_id, path):
        assert path == database
        assert agent_id not in opened
        session = original_open(agent_id, path)
        original_close = session.close

        def close():
            assert "child stopped" in lifecycle
            lifecycle.append(f"close:{agent_id}")
            original_close()

        monkeypatch.setattr(session, "close", Mock(side_effect=close))
        opened[agent_id] = session
        return session

    async def write(path, stream):
        assert isinstance(path, Path)
        assert path.parent == Path(output_store.WORKSPACE_SPILL_DIR)
        assert path.suffix == ".txt"
        writes[str(path)] = stream.read()

    runtime.sandbox.session = SimpleNamespace(write=AsyncMock(side_effect=write))
    monkeypatch.setattr(runner, "open_agent_session", open_session)

    async def fake_loop(agent, context, *, session, coordinator, **kwargs):
        service = context.services
        assert session is service.session_for("root") is service.session_for("root")
        child_session = service.session_for("child")
        assert child_session is service.session_for("child") and child_session is not session
        await child_session.add_items([{"role": "user", "content": "child started"}])
        await coordinator.register("child", "child", parent_id="root", task="local fixture")
        started = asyncio.Event()

        async def child():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await child_session.add_items([{"role": "assistant", "content": "child cleanup saved"}])
                lifecycle.append("child stopped")

        coordinator.attach_task("child", asyncio.create_task(child()))
        await started.wait()
        preview.append(await output_store.bound_and_store(full_output, max_lines=6, max_bytes=512))
        if outcome == "error":
            raise RuntimeError("fixture root failure")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return {"scan_completed": True, "success": True}

    original_teardown = runtime.sandbox.teardown.side_effect

    async def teardown(**kwargs):
        assert {"close:root", "close:child"}.issubset(lifecycle)
        lifecycle.append("sandbox teardown")
        await original_teardown(**kwargs)

    runtime.sandbox.teardown.side_effect = teardown
    monkeypatch.setattr(runner, "run_agent_loop", fake_loop)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await runner.run_scan(runtime.spec, runtime.settings)
    else:
        exit_code = await runner.run_scan(runtime.spec, runtime.settings)
        assert exit_code == (runner.EXIT_OK if outcome == "success" else runner.EXIT_FAILED)

    assert set(opened) == {"root", "child"}
    assert lifecycle[0] == "child stopped" and lifecycle[-1] == "sandbox teardown"
    for session in opened.values():
        session.close.assert_called_once()
        with pytest.raises(RuntimeError, match="closed"):
            await session.get_items()
    # Reopening the DB proves the child's final write survived cleanup.
    reopened = sessions.open_agent_session("child", database)
    try:
        assert [item["content"] for item in await reopened.get_items()] == [
            "child started",
            "child cleanup saved",
        ]
    finally:
        reopened.close()
    runtime.sandbox.session.write.assert_awaited_once()
    assert list(writes.values()) == [full_output.encode("utf-8")]
    assert len(preview[0].encode("utf-8")) <= 512
    assert next(iter(writes)) in preview[0]
    # After teardown, a later output cannot accidentally write into the old sandbox.
    after_teardown = await output_store.bound_and_store(full_output, max_lines=6, max_bytes=512)
    assert "full output saved" not in after_teardown
    runtime.sandbox.session.write.assert_awaited_once()
    assert _record(runtime)["status"] == ("completed" if outcome == "success" else "failed")


def _usage_events(runtime):
    return [
        event
        for line in (runtime.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if (event := json.loads(line))["event_type"] == "usage.updated"
    ]


def _install_usage_model(runtime):
    model = SimpleNamespace(
        model="fixture-model",
        get_response=AsyncMock(
            return_value=ModelResponse(
                output=[],
                response_id="response-usage-fixture",
                usage=Usage(input_tokens=137, output_tokens=29, total_tokens=166),
            )
        ),
    )
    runtime.make_model.return_value = model
    return model


async def _complete_tracked_response(context):
    model = context.services.model_for(runner.ROOT_AGENT_NAME)
    return await model.get_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
    )


@pytest.mark.parametrize("scan_type", ["web", "internal"])
async def test_completed_model_response_persists_usage_while_agent_loop_is_active(
    runtime, monkeypatch, scan_type
):
    model = _install_usage_model(runtime)
    response_completed = asyncio.Event()
    finish_loop = asyncio.Event()
    expected = {"requests": 1, "input_tokens": 137, "output_tokens": 29, "total_tokens": 166}

    async def fake_loop(agent, context, **kwargs):
        await _complete_tracked_response(context)
        response_completed.set()
        await finish_loop.wait()
        return {"scan_completed": True, "success": True}

    monkeypatch.setattr(runner, "run_agent_loop", fake_loop)
    spec = ScanSpec(target="fixture.invalid", scan_type=scan_type)
    task = asyncio.create_task(runner.run_scan(spec, runtime.settings))
    try:
        await asyncio.wait_for(response_completed.wait(), timeout=5)
        assert not task.done()
        record = _record(runtime)
        assert record["status"] == "running"
        assert "end_time" not in record
        assert record["llm_usage"] == expected
        assert [event["payload"] for event in _usage_events(runtime)] == [{"agent_id": "", **expected}]
    finally:
        finish_loop.set()
        exit_code = await asyncio.wait_for(task, timeout=5)

    assert exit_code == runner.EXIT_OK
    assert _record(runtime)["llm_usage"] == expected
    assert len(_usage_events(runtime)) == 1
    model.get_response.assert_awaited_once()


@pytest.mark.parametrize("scan_type", ["web", "internal"])
@pytest.mark.parametrize("outcome", ["error", "cancel"])
async def test_pending_usage_write_retried_at_scan_teardown_after_failure(
    runtime, monkeypatch, scan_type, outcome, caplog
):
    model = _install_usage_model(runtime)
    expected = {"requests": 1, "input_tokens": 137, "output_tokens": 29, "total_tokens": 166}
    original_write = artifacts.write_run_record
    original_record_usage = RunState.record_usage
    usage_attempts = []
    failed_write = False

    def fail_first_usage_write(run_dir, record):
        nonlocal failed_write
        if "llm_usage" in record and not failed_write:
            failed_write = True
            raise OSError("fixture transient usage write failure")
        original_write(run_dir, record)

    def record_usage(state, agent_id, totals):
        usage_attempts.append((state.run_record["status"], dict(totals)))
        original_record_usage(state, agent_id, totals)

    async def fake_loop(agent, context, **kwargs):
        await _complete_tracked_response(context)
        # Accounting failure must not fail or retry this completed model call.
        assert failed_write
        assert "llm_usage" not in _record(runtime)
        assert not _usage_events(runtime)
        if outcome == "cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("fixture failure after completed response")

    monkeypatch.setattr(artifacts, "write_run_record", fail_first_usage_write)
    monkeypatch.setattr(RunState, "record_usage", record_usage)
    monkeypatch.setattr(runner, "run_agent_loop", fake_loop)
    spec = ScanSpec(target="fixture.invalid", scan_type=scan_type)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await runner.run_scan(spec, runtime.settings)
    else:
        assert await runner.run_scan(spec, runtime.settings) == runner.EXIT_FAILED

    record = _record(runtime)
    assert record["status"] == "failed"
    assert record["llm_usage"] == expected
    assert record["failure_reason"] == (
        "interrupted" if outcome == "cancel" else "RuntimeError: fixture failure after completed response"
    )
    assert usage_attempts == [("running", expected), ("failed", expected)]
    assert [event["payload"] for event in _usage_events(runtime)] == [{"agent_id": "", **expected}]
    assert runtime.teardown_records[0]["llm_usage"] == expected
    assert runtime.teardown_records[0]["status"] == "failed"
    model.get_response.assert_awaited_once()
    assert "Failed to persist model usage" in caplog.text
