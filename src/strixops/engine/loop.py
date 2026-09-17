"""Per-agent execution loop with lifecycle detection and recovery nudges.

Transient interrupted model streams resume from the saved session, with bounded
backoff and no replay of completed tools. A turn that ends without a lifecycle
tool call (``finish_scan`` for root, ``agent_finish`` for children) does not end
the agent — the loop injects a recovery nudge as a user message in the agent's
session and runs again (bounded by ``MAX_NUDGES``). Exhausted recovery or an
unrecoverable exception terminates the agent as crashed/failed, with a notice
posted to the parent's mailbox so waiting parents never hang.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

from agents import Agent, RunConfig, Runner
from agents.memory import Session
from agents.run_config import CallModelData, ModelInputData

from strixops.config.context import ContextSettings
from strixops.config.model_errors import format_model_error
from strixops.engine import compaction, resilience
from strixops.engine.context_budget import context_limit_from_error
from strixops.engine.coordinator import STATUS_CRASHED, STATUS_FAILED, AgentCoordinator
from strixops.engine.scanconfig import EngineContext
from strixops.engine.sessions import (
    enforce_image_budget,
    open_agent_session,
    seed_initial_input,
    session_write_lock,
    strip_all_images_from_session,
)
from strixops.engine.stream_cleanup import consume_stream
from strixops.platform.events import EventWriter

MAX_NUDGES = 5
MAX_TRANSPORT_RECOVERIES = 5
# Consecutive compactions without a completed model turn; real progress resets it.
MAX_COMPACTIONS_PER_CYCLE = 2
MAX_IMAGE_STRIPS_PER_CYCLE = 3
_INPUT_REJECTION_CODES = frozenset({400, 404, 422})
logger = logging.getLogger(__name__)

# A real pentest agent routinely runs hundreds of tool calls (each call is a
# turn); the first live scan died everywhere on 60. Override: STRIXOPS_MAX_TURNS.
DEFAULT_MAX_TURNS = int((os.environ.get("STRIXOPS_MAX_TURNS") or "500").strip() or 500)

NUDGE_TEMPLATE = (
    "Your previous response ended the run without calling a lifecycle tool. "
    "You are an autonomous agent in a non-interactive scan: plain text does not "
    "end your work. Continue the task and call exactly one tool now. "
    "(recovery attempt {attempt}/{max_attempts})"
)


def run_config(sandbox: Any = None) -> RunConfig:
    # RunConfig is mutable shared state in the SDK; hand out fresh copies.
    kwargs: dict[str, Any] = {
        "tool_not_found_behavior": "return_error_to_model",
        "tracing_disabled": True,
        "model_settings": resilience.model_settings(),
    }
    if sandbox is not None:
        from agents.sandbox import SandboxRunConfig

        kwargs["sandbox"] = SandboxRunConfig(client=sandbox.client, session=sandbox.session)
    return RunConfig(**kwargs)


async def run_agent_loop(
    agent: Agent[EngineContext],
    context: EngineContext,
    *,
    initial_input: Any,
    events: EventWriter,
    coordinator: AgentCoordinator,
    max_turns: int = DEFAULT_MAX_TURNS,
    sandbox: Any = None,
    usage_sink: Any = None,
    session: Session | None = None,
) -> dict | None:
    """Drive an agent using the reference's session-backed execution cycles.

    The run owns sessions supplied by ``session_for``; standalone callers get
    an owned session closed here. Initial input is committed exactly once.
    """
    owned = False
    if session is None:
        session_for = getattr(context.services, "session_for", None)
        if session_for is not None:
            session = session_for(context.agent_id)
        else:
            session = open_agent_session(context.agent_id, coordinator.run_dir / ".state" / "agents.db")
            owned = True
    try:
        await seed_initial_input(session, initial_input)
        return await _run_agent_cycles(
            agent,
            context,
            session=session,
            events=events,
            coordinator=coordinator,
            max_turns=max_turns,
            sandbox=sandbox,
            usage_sink=usage_sink,
        )
    finally:
        if owned:
            session.close()


def _tools_text(agent: Any) -> str:
    return "\n".join(
        f"{getattr(tool, 'name', '')} {getattr(tool, 'description', '') or ''} "
        f"{getattr(tool, 'params_json_schema', '') or ''}"
        for tool in getattr(agent, "tools", []) or []
    )


def _context_guard(
    settings: ContextSettings,
    *,
    context_window_limit: int | None,
    has_pending_input: bool,
    allow_reserved_input: bool = False,
):
    first_call = True

    def check(data: CallModelData[Any]) -> ModelInputData:
        nonlocal first_call, allow_reserved_input
        # SDK 0.19 persists new run input AFTER this filter. Let new hints and
        # nudges reach that save point before using an exception to restart.
        # Initial task input is already seeded; later completed tool turns are
        # also saved before this filter runs. Never rewrite SDK-owned input here.
        skip = first_call and has_pending_input
        first_call = False
        model_name = getattr(getattr(data.agent, "model", None), "model", "")
        if skip or not settings.auto_compact or not isinstance(model_name, str) or not model_name:
            return data.model_data
        budget = compaction.input_budget(model_name, settings, context_window_limit)
        used = compaction.estimate_input_tokens(
            model_name, data.model_data.input, data.model_data.instructions or "", _tools_text(data.agent)
        )
        permitted_once = allow_reserved_input
        allow_reserved_input = False
        if used > budget:
            # A failed proactive summary need not make the soft threshold fatal.
            # Recheck the actual request, leaving output room and estimation margin.
            # This permission applies to one model call, never a whole Runner cycle.
            if permitted_once and used <= compaction.retry_input_budget(
                model_name, settings, context_window_limit
            ):
                return data.model_data
            raise compaction.ContextBudgetExceeded(used, budget)
        return data.model_data

    return check


async def _compact_session(
    agent: Any,
    session: Session,
    settings: ContextSettings,
    *,
    force: bool,
    context_window_limit: int | None = None,
    diagnostics: compaction.CompactionDiagnostics | None = None,
) -> bool:
    # Models live on the agent, wrapped for usage accounting. A bare model
    # string in RunConfig would change routing, so retain the existing client.
    model = getattr(agent, "model", None)
    model_name = getattr(model, "model", "")
    if not isinstance(model_name, str) or not model_name or not hasattr(model, "get_response"):
        if diagnostics is not None:
            diagnostics.status = "unsupported_model"
            diagnostics.detail = "The configured model does not support session summarization."
        return False
    instructions = getattr(agent, "instructions", "")
    return await compaction.maybe_compact(
        session,
        model=model_name,
        summary_model=model,
        instructions=instructions if isinstance(instructions, str) else "",
        tools_text=_tools_text(agent),
        force=force,
        settings=settings,
        context_window_limit=context_window_limit,
        diagnostics=diagnostics,
    )


async def _wait_for_transport_retry(delay: float) -> None:
    # An ordinary cancellable sleep keeps Stop responsive during backoff.
    await asyncio.sleep(delay)


async def _run_agent_cycles(
    agent: Agent[EngineContext],
    context: EngineContext,
    *,
    session: Session,
    events: EventWriter,
    coordinator: AgentCoordinator,
    max_turns: int,
    sandbox: Any,
    usage_sink: Any,
) -> dict | None:
    settings = getattr(context.services, "context_settings", None) or ContextSettings()
    input_items: list[Any] = []
    last_error: str | None = None
    nudges_used = 0
    compactions_used = 0
    context_recovery_attempts = 0
    image_strips_used = 0
    execution_attempts = 0
    consecutive_transport_recoveries = 0
    transport_recoveries = 0
    completed_model_turns = 0
    observed_context_limit: int | None = None
    reserved_input_pending = False
    reserved_input_used = False
    context.failure_reason = ""
    context.lifecycle_completion = None

    while True:
        result = None
        context_failure_detail = None
        proactive_failure = None
        try:
            execution_attempts += 1
            if reserved_input_used and input_items:
                # The SDK normally persists fresh input after its first filter.
                # After our one permitted deferral, save new hints/nudges here so
                # that persistence exception cannot also bypass the next check.
                async with session_write_lock(session):
                    await session.add_items(input_items)
                input_items = []
            try:
                await enforce_image_budget(session, settings.max_context_images)
            except Exception:
                logger.exception("image-budget enforcement failed for %s", context.agent_id)
            if not reserved_input_pending:
                try:
                    proactive_diagnostics = compaction.CompactionDiagnostics()
                    compact_options = (
                        {"context_window_limit": observed_context_limit} if observed_context_limit else {}
                    )
                    if await _compact_session(
                        agent, session, settings, force=False,
                        diagnostics=proactive_diagnostics, **compact_options,
                    ):
                        reserved_input_used = False
                    elif proactive_diagnostics.summary_attempts:
                        proactive_failure = proactive_diagnostics
                except Exception:
                    logger.exception("proactive compaction failed for %s", context.agent_id)
            config = run_config(sandbox)
            config.call_model_input_filter = _context_guard(
                settings,
                context_window_limit=observed_context_limit,
                has_pending_input=bool(input_items),
                allow_reserved_input=reserved_input_pending,
            )
            reserved_input_pending = False
            result = Runner.run_streamed(
                agent,
                input=input_items,
                context=context,
                max_turns=max_turns - completed_model_turns,
                run_config=config,
                session=session,
            )
            coordinator.attach_stream(context.agent_id, result)
            try:
                await consume_stream(
                    result,
                    lambda event: events.sdk_event(context.agent_id, context.agent_name, event),
                )
                if getattr(result, "run_loop_exception", None) is not None:
                    raise result.run_loop_exception
            finally:
                coordinator.detach_stream(context.agent_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — error ladder owns agent fate
            if result is not None:
                # Recovery continues this logical cycle. Count only completed
                # turns so disconnects cannot renew its tool/model turn budget.
                completed_model_turns += len(getattr(result, "raw_responses", []))
            context_overflow = compaction.is_context_overflow(exc)
            if context_overflow:
                if not isinstance(exc, compaction.ContextBudgetExceeded):
                    # Preserve the provider rejection even if removing images
                    # below allows an earlier retry of the same saved history.
                    reserved_input_used = True
                limit = context_limit_from_error(exc)
                if limit is not None:
                    observed_context_limit = min(observed_context_limit or limit, limit)
                # Real completed turns constitute progress. Bound consecutive
                # recovery failures without limiting compaction across a long scan.
                if result is not None and result.raw_responses:
                    compactions_used = 0
            if (
                image_strips_used < MAX_IMAGE_STRIPS_PER_CYCLE
                and getattr(exc, "status_code", None) in _INPUT_REJECTION_CODES
            ):
                try:
                    stripped = await strip_all_images_from_session(session)
                except Exception:
                    logger.exception("image-strip recovery failed for %s", context.agent_id)
                    stripped = False
                if stripped:
                    image_strips_used += 1
                    logger.info(
                        "Stripped images from %s session after rejection; retrying (%d)",
                        context.agent_id,
                        image_strips_used,
                    )
                    input_items = []
                    continue
            if compactions_used < MAX_COMPACTIONS_PER_CYCLE and context_overflow:
                compactions_used += 1
                context_recovery_attempts += 1
                reuse_failure = (
                    proactive_failure is not None
                    and isinstance(exc, compaction.ContextBudgetExceeded)
                    and exc.used_tokens == proactive_failure.input_tokens
                    and not getattr(result, "raw_responses", [])
                )
                trigger = (
                    "Input budget reached" if isinstance(exc, compaction.ContextBudgetExceeded)
                    else "Provider context overflow"
                )
                action = "evaluating failed summary" if reuse_failure else "summarizing saved session"
                notice = (
                    f"[context recovery] {trigger}; "
                    f"{action} (attempt {compactions_used}/{MAX_COMPACTIONS_PER_CYCLE}"
                    f"{f', provider limit={observed_context_limit}' if observed_context_limit else ''})"
                )
                print(notice, flush=True)
                events.emit(
                    event_type="chat.message", payload={"content": notice},
                    agent_id=context.agent_id, agent_name=context.agent_name,
                )
                diagnostics = proactive_failure if reuse_failure else compaction.CompactionDiagnostics()
                try:
                    compact_options = (
                        {"context_window_limit": observed_context_limit} if observed_context_limit else {}
                    )
                    compacted = False if reuse_failure else await _compact_session(
                        agent, session, settings, force=True, diagnostics=diagnostics, **compact_options
                    )
                except Exception as recovery_error:
                    logger.exception("overflow compaction failed for %s", context.agent_id)
                    diagnostics.status = "compaction_error"
                    diagnostics.detail = format_model_error(recovery_error)
                    compacted = False
                if compacted:
                    reserved_input_used = False
                    # The SDK persisted this cycle's input and completed tools.
                    # Resume the rewritten session without replaying old input.
                    input_items = []
                    events.emit(
                        event_type="chat.message",
                        payload={
                            "content": f"[context compacted] session summarized to fit the "
                            f"model context window (compaction {compactions_used}/"
                            f"{MAX_COMPACTIONS_PER_CYCLE})"
                        },
                        agent_id=context.agent_id,
                        agent_name=context.agent_name,
                    )
                    continue
                failure = (
                    f"status={diagnostics.status}, summary_attempts={diagnostics.summary_attempts}"
                    f"; {diagnostics.detail or 'No checkpoint was committed.'}"
                )
                context_failure_detail = (
                    f"Context recovery failed ({failure}); trigger: {format_model_error(exc)}"
                )
                notice = f"[context recovery failed] {failure}; saved session retained."
                print(notice, flush=True)
                events.emit(
                    event_type="chat.message", payload={"content": notice},
                    agent_id=context.agent_id, agent_name=context.agent_name,
                )
                if diagnostics.status == "session_changed" and compactions_used < MAX_COMPACTIONS_PER_CYCLE:
                    # Retry from the current persisted history, preserving any
                    # operator message that arrived while the summary was generated.
                    input_items = []
                    continue
                model_name = getattr(getattr(agent, "model", None), "model", "")
                if (
                    isinstance(exc, compaction.ContextBudgetExceeded)
                    and not reserved_input_used
                    and diagnostics.retryable
                    and diagnostics.status in {
                        "timeout", "transport_error", "output_limit", "empty_content",
                        "incomplete_output", "not_smaller",
                    }
                    and isinstance(model_name, str) and model_name
                    and exc.used_tokens <= compaction.retry_input_budget(
                        model_name, settings, observed_context_limit
                    )
                ):
                    reserved_input_pending = True
                    reserved_input_used = True
                    input_items = []
                    notice = (
                        "[context deferred] Summary unavailable; retrying one model request within "
                        "the remaining context reserve. Compaction will be checked again on the next turn."
                    )
                    print(notice, flush=True)
                    events.emit(
                        event_type="chat.message", payload={"content": notice},
                        agent_id=context.agent_id, agent_name=context.agent_name,
                    )
                    continue
            if resilience.can_resume_model_stream(exc):
                # A completed model turn in this cycle is real forward progress.
                # Shared model/usage counters include other agents and cannot
                # establish progress for this agent's recovery budget.
                if result is not None and result.raw_responses:
                    consecutive_transport_recoveries = 0
                if consecutive_transport_recoveries < MAX_TRANSPORT_RECOVERIES:
                    consecutive_transport_recoveries += 1
                    transport_recoveries += 1
                    delay = resilience.transport_recovery_delay(consecutive_transport_recoveries)
                    # consume_stream has joined the old producer and its cleanup.
                    # The SDK saved cycle input and completed tool results in the
                    # session; omit old hints/nudges and any unfinished output.
                    input_items = []
                    notice = (
                        f"[model retry] Model stream interrupted ({type(exc).__name__}); "
                        f"resuming saved session in {delay:g}s "
                        f"(retry {consecutive_transport_recoveries}/{MAX_TRANSPORT_RECOVERIES})"
                    )
                    print(notice, flush=True)
                    events.emit(
                        event_type="chat.message",
                        payload={"content": notice},
                        agent_id=context.agent_id,
                        agent_name=context.agent_name,
                    )
                    await _wait_for_transport_retry(delay)
                    continue
            last_error = context_failure_detail or format_model_error(exc)
            break

        consecutive_transport_recoveries = 0
        last_error = None
        # Retry any pending usage write; completed responses already published totals.
        if usage_sink is not None:
            with contextlib.suppress(Exception):
                usage_sink.record_turn(context.agent_id)

        # Tool output and assistant text share final_output in the SDK. Only
        # the successful lifecycle implementation may authorize completion.
        completion = context.lifecycle_completion
        expected_tool = "finish_scan" if context.parent_id is None else "agent_finish"
        expected_flag = "scan_completed" if context.parent_id is None else "agent_finished"
        if (
            completion is not None
            and completion.tool_name == expected_tool
            and completion.payload.get("success") is True
            and completion.payload.get(expected_flag) is True
        ):
            return completion.payload

        # A nudge/mailbox wake begins a new cycle, as in the reference runner.
        compactions_used = 0
        image_strips_used = 0
        completed_model_turns = 0

        # Wake path: mailbox messages (operator hints, peer notes) arrived —
        # append only new messages; does NOT consume recovery budget.
        pending = coordinator.drain_messages(context.agent_id)
        if pending:
            input_items = _message_input_items(pending)
            continue

        if nudges_used >= MAX_NUDGES:
            break
        nudges_used += 1
        # Session already contains this cycle; append the new nudge only.
        nudge = NUDGE_TEMPLATE.format(attempt=nudges_used, max_attempts=MAX_NUDGES)
        input_items = [{"role": "user", "content": nudge}]

    recovery_summary = f"{nudges_used} recovery nudges"
    if context_recovery_attempts:
        recovery_summary += f", {context_recovery_attempts} context recovery attempts"
    if transport_recoveries:
        recovery_summary += f", {transport_recoveries} transport recoveries"
    crash_reason = (
        f"Agent {context.agent_name} ({context.agent_id}) ended without a lifecycle tool "
        f"after {execution_attempts} attempt{'s' if execution_attempts != 1 else ''} "
        f"({recovery_summary})" + (f"; last error: {last_error}" if last_error else "")
    )
    context.failure_reason = crash_reason
    print(f"[agent crashed] {crash_reason}", flush=True)
    await coordinator.set_status(context.agent_id, STATUS_CRASHED)
    # Root has no parent mailbox — emit the reason as a visible message so it
    # reaches the run log and the platform conversation view.
    events.emit(
        event_type="chat.message",
        payload={"content": f"[agent crashed] {crash_reason}"},
        agent_id=context.agent_id,
        agent_name=context.agent_name,
    )
    await _notify_parent(
        coordinator,
        parent_id=context.parent_id,
        from_id=context.agent_id,
        from_name=context.agent_name,
        kind="agent_crashed",
        text=crash_reason,
    )
    return None


async def mark_agent_failed(
    coordinator: AgentCoordinator,
    context: EngineContext,
    reason: str,
) -> None:
    await coordinator.set_status(context.agent_id, STATUS_FAILED)
    await _notify_parent(
        coordinator,
        parent_id=context.parent_id,
        from_id=context.agent_id,
        from_name=context.agent_name,
        kind="agent_failed",
        text=f"Agent {context.agent_name} ({context.agent_id}) failed: {reason}",
    )


def _message_input_items(messages: list[dict[str, Any]]) -> list[Any]:
    """New mailbox messages rendered as user items for the existing session.

    Operator/user messages pass through as-is; peer agent messages get a
    provenance header so the model knows who is talking.
    """
    items: list[Any] = []
    for message in messages:
        sender = str(message.get("from") or "")
        kind = str(message.get("type") or "info")
        content = str(message.get("content") or "")
        if message.get("from") in ("operator", "user") or kind == "operator_hint":
            items.append({"role": "user", "content": content})
            continue
        from_name = message.get("from_name") or sender
        priority = message.get("priority", "normal")
        header = f"[Message from {from_name} ({sender}) | type={kind} | priority={priority}]"
        items.append({"role": "user", "content": f"{header}\n{content}"})
    return items


async def _notify_parent(
    coordinator: AgentCoordinator,
    *,
    parent_id: str | None,
    from_id: str,
    from_name: str,
    kind: str,
    text: str,
) -> None:
    if not parent_id:
        return
    await coordinator.send(
        parent_id,
        {
            "from": from_id,
            "from_name": from_name,
            "type": kind,
            "priority": "high",
            "content": text,
        },
    )
