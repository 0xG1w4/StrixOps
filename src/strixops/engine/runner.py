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
import dataclasses
import io
import json
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


def _evidence_failure_detail(evidence: dict[str, Any]) -> str:
    """Explain an incomplete delivery without exposing arbitrary exception bodies."""
    detail = (
        f"{evidence.get('count', 0)} file(s) delivered, "
        f"{evidence.get('failed_count', 0)} file(s) not delivered, "
        f"{evidence.get('missing_reference_count', 0)} unresolved reference(s), "
        f"{len(evidence.get('errors', []))} collection error(s)"
    )
    missing = [ref for ref in evidence.get("references", []) if not ref.get("deliverable")]
    if missing:
        # JSON quoting keeps untrusted filenames from injecting terminal lines.
        detail += "; unresolved: " + ", ".join(
            json.dumps(str(ref.get("reference", ""))[:200], ensure_ascii=False)
            for ref in missing[:3]
        )
    return detail + "; see run.json evidence and evidence/.evidence_index.json"


async def run_scan(spec: ScanSpec, settings: EngineSettings) -> int:
    targets = spec.all_targets()
    spec = dataclasses.replace(spec, target=targets[0], targets=targets if len(targets) > 1 else [])
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
    if not settings.dry_run:
        from strixops.config.model_options import API_MODES, resolved_api_mode

        scan_config["llm_api_mode_requested"] = settings.llm_api_mode
        scan_config["llm_reasoning_effort"] = settings.llm_reasoning_effort
        if settings.llm_api_mode in API_MODES:
            scan_config["llm_api_mode"] = resolved_api_mode(settings.strix_llm, settings.llm_api_mode)
    events.run_configured(scan_config)
    _stdout_log(f"StrixOps run {run_name} starting (scan_type={spec.scan_type}, target={spec.target})")
    if len(targets) > 1:
        _stdout_log(f"Multi-target run: {len(targets)} targets share this run and report.")

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
    scan_succeeded = False
    interrupted = False
    cleanup_errors: list[dict[str, str]] = []

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

        # The whole finalizer is shielded and joined below. Its copy cannot be
        # orphaned by repeated cancellation of the outer scan task.
        try:
            return await asyncio.to_thread(collect)
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

    # Bound by the try below once the model route comes up; stays None on the
    # fatal startup paths so finalize never references an unbound name.
    model_for: Any = None

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

        if payload and payload.get("scan_completed"):
            # The agent's finish intent is not a successful run until evidence
            # delivery and sandbox cleanup have also finished.
            scan_succeeded = True
        else:
            failure_reason = (
                root_context.failure_reason or "root agent ended without a successful finish_scan"
            )
            run_state.mark_failed(failure_reason)
            await coordinator.set_status("root", STATUS_FAILED)
            _stdout_log(f"Scan failed: {failure_reason}")

    except asyncio.CancelledError:
        # SIGTERM/SIGINT (the cli installs the handlers): asyncio.run's cleanup
        # cancels this task — record the interruption before the shared
        # teardown below runs, then let the cancellation propagate.
        _stdout_log("interrupted — marking run failed")
        run_state.mark_failed("interrupted")
        interrupted = True
    except Exception as exc:  # noqa: BLE001 — never exit without artifacts/state
        _stdout_log(f"fatal: {type(exc).__name__}: {exc}")
        run_state.mark_failed(f"{type(exc).__name__}: {exc}")
    finally:

        def cleanup_failed(stage: str, exc: BaseException) -> None:
            detail = (
                _evidence_failure_detail(run_state.run_record.get("evidence", {}))
                if stage == "evidence" else ""
            )
            cleanup_errors.append({
                "stage": stage, "error_type": type(exc).__name__,
                **({"message": detail} if detail else {}),
            })
            logger.error(
                "scan cleanup failed at %s (%s)%s", stage, type(exc).__name__,
                f": {detail}" if detail else "",
            )
            # Preserve the original failure/interruption and record teardown
            # errors separately. Even a state write failure must not skip delete.
            reason = (
                run_state.run_record.get("failure_reason")
                or f"cleanup failed: {stage} ({type(exc).__name__})" + (f": {detail}" if detail else "")
            )
            with contextlib.suppress(Exception):
                run_state.mark_failed(str(reason))

        async def finalize() -> None:
            run_state.run_record["cleanup"] = {"status": "in_progress", "errors": cleanup_errors}
            if hints_poller is not None:
                try:
                    hints_poller.stop()
                except Exception as exc:
                    cleanup_failed("hints", exc)
            if hints_task is not None:
                hints_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await hints_task
            agents_settled = False
            try:
                await _reap_children(coordinator)
                agents_settled = True
            except BaseException as exc:
                cleanup_failed("agents", exc)
            try:
                usage_accumulator.flush()
            except Exception as exc:
                cleanup_failed("usage", exc)

            writers_stopped = sandbox is None
            if sandbox is not None:
                try:
                    # Legacy test doubles may lack quiesce; every real bundle
                    # implements the verified Docker stop boundary.
                    quiesce = getattr(sandbox, "quiesce", None)
                    if quiesce is not None:
                        if run_state.run_record.get("status") == "failed":
                            await quiesce(diagnostics_dir=run_dir / "diagnostics")
                        else:
                            await quiesce()
                    writers_stopped = True
                except BaseException as exc:
                    cleanup_failed("sandbox_quiesce", exc)
                finally:
                    if isinstance(getattr(sandbox, "quiescence", None), dict):
                        run_state.run_record["cleanup"]["quiescence"] = dict(sandbox.quiescence)

            if agents_settled and writers_stopped:
                try:
                    await collect_run_evidence()
                    if run_state.run_record.get("evidence", {}).get("status") == "incomplete":
                        cleanup_failed("evidence", RuntimeError("Evidence delivery incomplete"))
                except BaseException as exc:
                    cleanup_failed("evidence", exc)
            elif workspace_dir is not None:
                # No snapshot can claim stable evidence while a writer might
                # still be active. Keep the source workspace for recovery.
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
                    "errors": [
                        "Evidence collection skipped: writer shutdown was not confirmed; workspace retained."
                    ],
                }
            try:
                # A failed/interrupted scan may still have a useful final draft.
                # Live runs compose the deliverable from the full findings
                # corpus via a dedicated synthesis call (the platform's report
                # worker, ported); anything else — dry run, model failure,
                # timeout — falls back to the deterministic composer so the
                # run never ends without a report file.
                synthesized = None
                if not settings.dry_run and model_for is not None:
                    from strixops.report.synthesis import synthesis_enabled, synthesize_executive_report

                    if synthesis_enabled():
                        synthesized = await synthesize_executive_report(
                            run_state,
                            lambda: model_for("report-synthesis"),
                        )
                if synthesized is not None:
                    from strixops.platform import artifacts

                    artifacts.write_executive_report(run_state.run_dir, synthesized)
                    run_state.run_record["report_synthesized"] = True
                    run_state.events.emit(
                        event_type="report.synthesized",
                        payload={"mode": "llm", "chars": len(synthesized)},
                        agent_name="report synthesis",
                    )
                else:
                    run_state.write_executive_report()
                    run_state.run_record["report_synthesized"] = False
            except Exception as exc:
                cleanup_failed("report", exc)
            try:
                run_state.save()
            except Exception as exc:
                cleanup_failed("state", exc)
            if agents_settled:
                for session in agent_sessions.values():
                    try:
                        session.close()
                    except Exception as exc:
                        cleanup_failed("session", exc)
            configure_spill_writer(None)
            if sandbox is not None:
                try:
                    if run_state.run_record.get("status") == "failed":
                        await sandbox.teardown(diagnostics_dir=run_dir / "diagnostics")
                    else:
                        await sandbox.teardown()
                except BaseException as exc:
                    cleanup_failed("sandbox_delete", exc)
                finally:
                    if isinstance(getattr(sandbox, "cleanup", None), dict):
                        run_state.run_record["cleanup"]["sandbox"] = dict(sandbox.cleanup)
            if gateway is not None:
                try:
                    await asyncio.to_thread(gateway.stop)
                except Exception as exc:
                    cleanup_failed("gateway", exc)
            try:
                await coordinator.set_status(
                    "root",
                    STATUS_COMPLETED
                    if scan_succeeded and not interrupted and not cleanup_errors
                    else STATUS_FAILED,
                )
            except BaseException as exc:
                cleanup_failed("agent_status", exc)
            run_state.run_record["cleanup"]["status"] = "failed" if cleanup_errors else "complete"
            try:
                run_state.save()
            except Exception as exc:
                run_state.run_record["cleanup"]["status"] = "failed"
                cleanup_failed("state", exc)

        cleanup_task = asyncio.create_task(finalize(), name="strixops-run-finalize")
        while True:
            try:
                await asyncio.shield(cleanup_task)
                break
            except asyncio.CancelledError:
                # Repeated SIGTERM/SIGINT only cancels this waiter, never the
                # task writing evidence or removing the owned container.
                interrupted = True
                with contextlib.suppress(Exception):
                    run_state.mark_failed("interrupted")
                if cleanup_task.done():
                    cleanup_task.result()
                    break
        if interrupted:
            raise asyncio.CancelledError
    if scan_succeeded and not cleanup_errors:
        try:
            run_state.mark_complete()
        except Exception as exc:
            _stdout_log(f"Completion could not be persisted: {type(exc).__name__}")
            with contextlib.suppress(Exception):
                run_state.mark_failed(f"completion failed: {type(exc).__name__}")
            return EXIT_FAILED
        evidence_count = int(run_state.run_record.get("evidence", {}).get("count", 0))
        evidence_note = f", {evidence_count} evidence file(s)" if evidence_count else ""
        _stdout_log(
            f"Scan completed: {len(run_state.reports)} finding(s), "
            f"{run_state.duration_seconds()}s{evidence_note}"
        )
        return EXIT_OK
    return EXIT_FAILED


async def _reap_children(coordinator: AgentCoordinator) -> None:
    await coordinator.quiesce()
