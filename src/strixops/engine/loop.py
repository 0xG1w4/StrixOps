"""Per-agent execution loop with lifecycle detection and recovery nudges.

A turn that ends without a lifecycle tool call (``finish_scan`` for root,
``agent_finish`` for children) does not end the agent — the loop injects a
recovery nudge as a user message in the agent's session, and
runs again (bounded by ``MAX_NUDGES``). Exhausted nudges or a raised
exception terminate the agent as crashed/failed, with a notice posted to the
parent's mailbox so waiting parents never hang.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

from agents import Agent, RunConfig, Runner
from agents.memory import Session

from strixops.config.context import ContextSettings
from strixops.engine import compaction, resilience
from strixops.engine.coordinator import STATUS_CRASHED, STATUS_FAILED, AgentCoordinator
from strixops.engine.scanconfig import EngineContext
from strixops.engine.sessions import (
    enforce_image_budget,
    open_agent_session,
    seed_initial_input,
    strip_all_images_from_session,
)
from strixops.platform.events import EventWriter

MAX_NUDGES = 5
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


async def _compact_session(
    agent: Any,
    session: Session,
    settings: ContextSettings,
    *,
    force: bool,
) -> bool:
    # Models live on the agent, wrapped for usage accounting. A bare model
    # string in RunConfig would change routing, so retain the existing client.
    model = getattr(agent, "model", None)
    model_name = getattr(model, "model", "")
    if not isinstance(model_name, str) or not model_name or not hasattr(model, "get_response"):
        return False
    instructions = getattr(agent, "instructions", "")
    tools_text = "\n".join(
        f"{getattr(tool, 'name', '')} {getattr(tool, 'description', '') or ''} "
        f"{getattr(tool, 'params_json_schema', '') or ''}"
        for tool in getattr(agent, "tools", []) or []
    )
    return await compaction.maybe_compact(
        session,
        model=model_name,
        summary_model=model,
        instructions=instructions if isinstance(instructions, str) else "",
        tools_text=tools_text,
        force=force,
        settings=settings,
    )


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
    image_strips_used = 0
    execution_attempts = 0
    context.failure_reason = ""
    context.lifecycle_completion = None

    while True:
        try:
            execution_attempts += 1
            try:
                await enforce_image_budget(session, settings.max_context_images)
            except Exception:
                logger.exception("image-budget enforcement failed for %s", context.agent_id)
            try:
                await _compact_session(agent, session, settings, force=False)
            except Exception:
                logger.exception("proactive compaction failed for %s", context.agent_id)
            result = Runner.run_streamed(
                agent,
                input=input_items,
                context=context,
                max_turns=max_turns,
                run_config=run_config(sandbox),
                session=session,
            )
            coordinator.attach_stream(context.agent_id, result)
            try:
                async for stream_event in result.stream_events():
                    events.sdk_event(context.agent_id, context.agent_name, stream_event)
                if getattr(result, "run_loop_exception", None) is not None:
                    raise result.run_loop_exception
            finally:
                coordinator.detach_stream(context.agent_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — error ladder owns agent fate
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
            if compactions_used < MAX_COMPACTIONS_PER_CYCLE and compaction.is_context_overflow(exc):
                try:
                    compacted = await _compact_session(agent, session, settings, force=True)
                except Exception:
                    logger.exception("overflow compaction failed for %s", context.agent_id)
                    compacted = False
                if compacted:
                    compactions_used += 1
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
            last_error = f"{type(exc).__name__}: {exc}"
            break

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

    crash_reason = (
        f"Agent {context.agent_name} ({context.agent_id}) ended without a lifecycle tool "
        f"after {execution_attempts} attempt{'s' if execution_attempts != 1 else ''} "
        f"({nudges_used} recovery nudges)" + (f"; last error: {last_error}" if last_error else "")
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
