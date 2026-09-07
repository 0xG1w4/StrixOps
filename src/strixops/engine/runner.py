"""Scan orchestration — the run lifecycle from spawn to exit code.

Startup ordering is contract-critical: the run dir, an empty ``events.jsonl``,
and the ``run.configured`` event exist **before** any slow work (the
platform's supervisor must bind the run dir within 60 s of spawn and flips
the task to ``running`` on the first event line; Docker image pulls are
irrelevant to that window once the files exist).

Exit-code contract (platform ``supervisor.on_terminated``):
``0`` → reporting · non-zero with events → partial reporting · no events →
failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from pathlib import Path
from typing import Any

from strixops.agents.factory import ROOT_AGENT_NAME, build_root_agent
from strixops.config.context import ContextSettings
from strixops.config.settings import EngineSettings
from strixops.engine.coordinator import STATUS_COMPLETED, STATUS_FAILED, AgentCoordinator
from strixops.engine.loop import DEFAULT_MAX_TURNS, run_agent_loop
from strixops.engine.scanconfig import EngineContext, EngineServices, ScanSpec, build_root_task
from strixops.engine.sessions import open_agent_session
from strixops.engine.spawn import make_spawn_child
from strixops.platform.events import EventWriter
from strixops.platform.runname import create_run_dir, generate_run_name, resolve_runs_root
from strixops.report.state import RunState
from strixops.tools.output_store import WORKSPACE_SPILL_DIR, configure_spill_writer

MAX_TURNS = DEFAULT_MAX_TURNS

EXIT_OK = 0
EXIT_FAILED = 1
logger = logging.getLogger(__name__)


def _stdout_log(message: str) -> None:
    """Plain, flushed stdout — the supervisor pipes it into tui.log."""
    print(message, flush=True)


async def run_scan(spec: ScanSpec, settings: EngineSettings) -> int:
    # 1. Contract surface first: run dir + events.jsonl + run.configured.
    runs_root = resolve_runs_root(settings.strix_runs)
    run_name = generate_run_name(spec.target, spec.scan_type)
    run_dir = create_run_dir(runs_root, run_name)
    events = EventWriter(run_dir)
    scan_config = spec.as_scan_config()
    # Execution-mode facts the console's info surface reads from run.json:
    # dry-run flag and the model route the run actually used.
    scan_config["dry_run"] = settings.dry_run
    if settings.strix_llm:
        scan_config["model"] = settings.strix_llm
    events.run_configured(scan_config)
    _stdout_log(f"StrixOps run {run_name} starting (scan_type={spec.scan_type}, target={spec.target})")

    coordinator = AgentCoordinator(run_dir, events)
    run_state = RunState(run_dir, events)
    run_state.set_scan_config(scan_config)

    gateway = None
    sandbox = None
    hints_task: asyncio.Task | None = None
    hints_poller = None
    agent_sessions: dict[str, Any] = {}
    workspace_dir: Path | None = (
        Path(settings.host_workspace_dir).expanduser().resolve() if settings.host_workspace_dir else None
    )
    evidence_collected = False

    def prepare_workspace() -> str:
        nonlocal workspace_dir
        # Mount only this workspace, never the run directory containing trusted
        # state/report files. Retain it on every exit, including Docker startup
        # failures, so evidence recovery does not depend on a living container.
        workspace_dir = (
            Path(settings.host_workspace_dir).expanduser().resolve()
            if settings.host_workspace_dir
            else run_dir / "workspace"
        )
        workspace_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_state.run_record["workspace"] = {"path": str(workspace_dir), "persistent": True}
        run_state.save()
        return str(workspace_dir)

    async def collect_run_evidence() -> int:
        nonlocal evidence_collected
        if evidence_collected or workspace_dir is None:
            return int(run_state.run_record.get("evidence", {}).get("count", 0))

        def collect() -> int:
            try:
                return run_state.collect_evidence(workspace_dir)
            except Exception as exc:
                logger.exception("failed to persist workspace evidence")
                run_state.run_record["evidence"] = {
                    "status": "incomplete",
                    "count": 0,
                    "captured_count": 0,
                    "persisted_count": 0,
                    "failed_count": 0,
                    "total_bytes": 0,
                    "files": [],
                    "references": [],
                    "missing_reference_count": 0,
                    "errors": [f"Evidence collection failed: {type(exc).__name__}: {exc}"],
                }
                run_state.save()
                return 0

        # Shield the file writer, then join it on cancellation. Teardown must
        # never race an in-progress copy, and disk I/O must not block the loop.
        task = asyncio.create_task(asyncio.to_thread(collect))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Preserve cancellation even if recording a disk failure also fails.
            with contextlib.suppress(Exception):
                await task
            raise
        finally:
            evidence_collected = True

    def session_for(agent_id: str) -> Any:
        if agent_id not in agent_sessions:
            agent_sessions[agent_id] = open_agent_session(agent_id, run_dir / ".state" / "agents.db")
        return agent_sessions[agent_id]

    # Run-level token accounting: every model call flows through this wrapper.
    from strixops.engine.usage import UsageAccumulator, UsageTrackingModel

    usage_accumulator = UsageAccumulator(
        # The shared model includes root, child, dedupe and compaction calls.
        # These are run totals, so do not attribute the update to one agent.
        on_update=lambda totals: run_state.record_usage("", totals),
    )

    def _tracked(model: Any) -> Any:
        return UsageTrackingModel(model, usage_accumulator)

    class _UsageSink:
        """Retry pending usage writes at agent-cycle boundaries."""

        @staticmethod
        def record_turn(agent_id: str) -> None:
            usage_accumulator.flush()

    usage_sink = _UsageSink()

    try:
        context_settings = ContextSettings()
        if settings.dry_run:
            import os

            from strixops.testing.scripted_gateway import (
                ScriptedGateway,
                child_script,
                hint_scenario_scripts,
                internal_script,
                web_script,
                web_script_with_shell,
            )

            use_shell = (os.environ.get("STRIXOPS_DRY_RUN_SANDBOX") or "").strip() in {"1", "true", "yes"}
            if (os.environ.get("STRIXOPS_DRY_RUN_HINTS") or "").strip() in {"1", "true", "yes"}:
                scripts = hint_scenario_scripts()
            elif spec.scan_type == "internal":
                scripts = {
                    "strixops-dry-run": internal_script(spec.target, spec.socks5_proxy),
                    "child": child_script(spec.target),
                }
            else:
                root_script = web_script_with_shell(spec.target) if use_shell else web_script(spec.target)
                scripts = {"strixops-dry-run": root_script, "child": child_script(spec.target)}
            scripts["dedupe"] = [
                {
                    "text": '{"is_duplicate":false,"duplicate_id":"",'
                    '"confidence":1.0,"reason":"Distinct scripted fixture finding"}'
                }
            ]
            gateway = ScriptedGateway(scripts=scripts)
            gateway.start()

            def model_for(agent_name: str):
                if agent_name == ROOT_AGENT_NAME:
                    return _tracked(gateway.make_model("strixops-dry-run"))
                if agent_name == "dedupe":
                    return _tracked(gateway.make_model("dedupe"))
                return _tracked(gateway.make_model("child"))

            # Optional real-sandbox dry run (STRIXOPS_DRY_RUN_SANDBOX=1):
            # scripted model + real container, exercising the Shell capability.
            if (os.environ.get("STRIXOPS_DRY_RUN_SANDBOX") or "").strip() in {"1", "true", "yes"}:
                from strixops.runtime.sandbox import create_sandbox_session

                _stdout_log("dry run: bringing up real sandbox container")
                sandbox = await create_sandbox_session(
                    scan_type=spec.scan_type,
                    socks5_proxy=spec.socks5_proxy,
                    gsocket_key=spec.gsocket_key,
                    host_workspace_dir=prepare_workspace(),
                )
        else:
            problems = settings.validate()
            for problem in problems:
                _stdout_log(f"preflight: {problem}")
            if problems:
                run_state.mark_failed("preflight-failed")
                return EXIT_FAILED

            from strixops.config.provider import make_platform_model
            from strixops.runtime.sandbox import create_sandbox_session

            platform_model = _tracked(make_platform_model(settings))

            def model_for(agent_name: str):
                return platform_model

            _stdout_log("Bringing up sandbox container…")
            sandbox = await create_sandbox_session(
                scan_type=spec.scan_type,
                socks5_proxy=spec.socks5_proxy,
                gsocket_key=spec.gsocket_key,
                host_workspace_dir=prepare_workspace(),
                use_caido=spec.scan_type == "web",  # web scans: intercept HTTP via Caido
            )

        if sandbox is not None:
            _stdout_log("Sandbox ready; starting agent…")

            async def spill_to_workspace(output_id: str, text: str) -> str | None:
                path = f"{WORKSPACE_SPILL_DIR}/{output_id}.txt"
                try:
                    await sandbox.session.write(Path(path), io.BytesIO(text.encode("utf-8")))
                except Exception:
                    logger.exception("failed to spill tool output to sandbox workspace")
                    return None
                return path

            configure_spill_writer(spill_to_workspace)
            if getattr(sandbox, "caido", None) is not None:
                from strixops.runtime.proxy_binding import record_caido_runtime

                session_state = getattr(sandbox.session, "state", None)
                record_caido_runtime(run_dir, getattr(session_state, "container_id", ""))

        # 2. Engine services + root agent.
        root_input = build_root_task(spec)
        services = EngineServices(
            coordinator=coordinator,
            events=events,
            run_state=run_state,
            spec=spec,
            spawn_child=None,
            model_for=model_for,
            sandbox=sandbox,
            usage_sink=usage_sink,
            root_input=root_input,
            session_for=session_for,
            context_settings=context_settings,
        )
        services.spawn_child = make_spawn_child(services)

        root_context = EngineContext(
            agent_id="root",
            agent_name=ROOT_AGENT_NAME,
            parent_id=None,
            run_state=run_state,
            registry=coordinator,
            services=services,
        )
        agent = build_root_agent(
            spec, model=model_for(ROOT_AGENT_NAME), sandbox=sandbox is not None, run_dir=run_dir
        )
        await coordinator.register("root", ROOT_AGENT_NAME, parent_id=None, task=root_input)

        # Operator-hints poller: reads the platform's hint inbox and delivers
        # to the targeted agent (or root) with force-interrupt.
        from strixops.platform.hints import OperatorHintsPoller

        hints_poller = OperatorHintsPoller(settings.operator_hints_dir, coordinator)
        hints_task = asyncio.get_running_loop().create_task(hints_poller.run())

        # 3. Root loop drives the scan; children run as concurrent tasks.
        payload = await run_agent_loop(
            agent,
            root_context,
            initial_input=root_input,
            events=events,
            coordinator=coordinator,
            max_turns=MAX_TURNS,
            sandbox=sandbox,
            usage_sink=usage_sink,
            session=session_for("root"),
        )

        # 4. Teardown: reap any leftover child tasks (normally none —
        # finish_scan refuses while children are active).
        await _reap_children(coordinator)

        if payload and payload.get("scan_completed"):
            evidence_count = await collect_run_evidence()
            run_state.write_executive_report()
            if run_state.run_record.get("evidence", {}).get("status") == "incomplete":
                _stdout_log("Evidence delivery is incomplete; see run.json and the retained workspace.")
            run_state.mark_complete()
            await coordinator.set_status("root", STATUS_COMPLETED)
            evidence_note = f", {evidence_count} evidence file(s)" if evidence_count else ""
            _stdout_log(
                f"Scan completed: {len(run_state.reports)} finding(s), "
                f"{run_state.duration_seconds()}s{evidence_note}"
            )
            return EXIT_OK

        failure_reason = root_context.failure_reason or "root agent ended without a successful finish_scan"
        run_state.mark_failed(failure_reason)
        await coordinator.set_status("root", STATUS_FAILED)
        _stdout_log(f"Scan failed: {failure_reason}")
        return EXIT_FAILED

    except asyncio.CancelledError:
        # SIGTERM/SIGINT (the cli installs the handlers): asyncio.run's cleanup
        # cancels this task — record the interruption before the shared
        # teardown below runs, then let the cancellation propagate.
        _stdout_log("interrupted — marking run failed")
        run_state.mark_failed("interrupted")
        raise
    except Exception as exc:  # noqa: BLE001 — never exit without artifacts/state
        _stdout_log(f"fatal: {type(exc).__name__}: {exc}")
        run_state.mark_failed(f"{type(exc).__name__}: {exc}")
        return EXIT_FAILED
    finally:
        if hints_poller is not None:
            hints_poller.stop()
        if hints_task is not None:
            hints_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hints_task
        # A child may still own a running SDK stream after a root failure.
        # Stop those writers before closing their shared SQLite database.
        await _reap_children(coordinator)
        usage_accumulator.flush()
        await collect_run_evidence()
        # If a final draft exists after failure/interruption, keep a partial
        # report with the actual evidence delivery status for operator recovery.
        if run_state.run_record.get("status") == "failed":
            with contextlib.suppress(Exception):
                run_state.write_executive_report()
        for session in agent_sessions.values():
            with contextlib.suppress(Exception):
                session.close()
        configure_spill_writer(None)
        if sandbox is not None:
            if run_state.run_record.get("status") == "failed":
                await sandbox.teardown(diagnostics_dir=run_dir / "diagnostics")
            else:
                await sandbox.teardown()
        if gateway is not None:
            gateway.stop()


async def _reap_children(coordinator: AgentCoordinator) -> None:
    for agent_id in list(coordinator.agent_ids()):
        if agent_id == "root":
            continue
        await coordinator.cancel_agent(agent_id)
