"""Child-agent spawning: concurrent asyncio tasks with inherited context.

Each child gets its own ``EngineContext`` (own id/name, parent linkage), its
own model instance (dry-run: per-agent scripted model), and runs
``run_agent_loop`` as an independent task. The platform observes the tree
through ``agent.created`` (with ``parent_id``) and status transitions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from strixops import skills as skill_registry
from strixops.agents.factory import build_child_agent
from strixops.agents.prompts import engagement_context
from strixops.engine.coordinator import STATUS_COMPLETED, AgentCoordinator
from strixops.engine.loop import run_agent_loop
from strixops.engine.scanconfig import EngineContext, EngineServices
from strixops.engine.sessions import scrub_images_from_items
from strixops.platform.events import EventWriter

logger = logging.getLogger(__name__)


def make_spawn_child(services: EngineServices):
    """Build the ``spawn_child`` callable injected into every agent context."""

    def spawn_child(
        *,
        name: str,
        task: str,
        parent_history: list[Any],
        skills: list[str],
        parent: EngineContext,
    ) -> dict[str, Any]:
        coordinator: AgentCoordinator = services.coordinator  # type: ignore[assignment]
        events: EventWriter = services.events  # type: ignore[assignment]

        agent_id = uuid.uuid4().hex[:8]
        child_context = EngineContext(
            agent_id=agent_id,
            agent_name=name,
            parent_id=parent.agent_id,
            run_state=services.run_state,
            registry=coordinator,
            services=services,
        )
        model = services.model_for(name) if services.model_for is not None else None
        run_dir = getattr(services, "run_dir", None) or (
            services.run_state.run_dir if services.run_state is not None else None
        )
        try:
            skills = list(dict.fromkeys(skill_registry.canonical_skill_id(skill) for skill in skills))
            agent = build_child_agent(
                name,
                task,
                model=model,
                sandbox=services.sandbox is not None,
                spec=services.spec,
                skills=skills,
                run_dir=run_dir,
            )
        except skill_registry.SkillResolutionError as exc:
            return {"ok": False, "error": str(exc)}
        initial_input = _child_initial_input(services, name, agent_id, parent, task, parent_history)

        async def _run_child() -> None:
            try:
                await coordinator.snapshot()
                payload = await run_agent_loop(
                    agent,
                    child_context,
                    initial_input=initial_input,
                    events=events,
                    coordinator=coordinator,
                    sandbox=services.sandbox,
                    usage_sink=services.usage_sink,
                    session=services.session_for(agent_id) if services.session_for is not None else None,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — child failures never kill siblings
                events.emit(
                    event_type="chat.message",
                    payload={"content": f"agent {name} crashed: {type(exc).__name__}: {exc}"},
                    agent_id=agent_id,
                    agent_name=name,
                )
                try:
                    await coordinator.set_status(agent_id, "crashed")
                except Exception:
                    # set_status changes memory before persisting. A failed
                    # snapshot must not leave a failed child active forever.
                    logger.exception("Failed to persist crashed child %s", agent_id)
                return
            if payload and payload.get("agent_finished"):
                await coordinator.set_status(agent_id, STATUS_COMPLETED)
            # crash path (nudges exhausted) already set its status inside the loop

        loop = asyncio.get_running_loop()
        # No await between reservation, task attachment and the success reply:
        # a parent can immediately wait, finish or tear down safely.
        coordinator.reserve(agent_id, name, parent_id=parent.agent_id, task=task, skills=skills)
        task_obj = loop.create_task(_run_child(), name=f"agent-{agent_id}")
        coordinator.attach_task(agent_id, task_obj)
        return {"ok": True, "agent_id": agent_id, "name": name}

    return spawn_child


def _child_initial_input(
    services: EngineServices,
    name: str,
    agent_id: str,
    parent: EngineContext,
    task: str,
    parent_history: list[Any],
) -> list[dict[str, Any]]:
    """Keep inherited background and the new assignment in one user message.

    As in Strix, the history is the parent's SDK turn-input snapshot, not a
    live session or a replay of the parent's task. Image payloads are omitted
    from the serialized background and the child keeps its own identity.
    """
    parts: list[str] = []
    if parent_history:
        rendered = json.dumps(scrub_images_from_items(parent_history), ensure_ascii=False, default=str)
        parts.append(
            "== Inherited context from parent (background only) ==\n"
            f"{rendered}\n"
            "== End of inherited context ==\n"
            "Use the above as background only; do not continue the "
            "parent's work. Your task follows."
        )
    parts.append(
        f"You are agent {name} ({agent_id}); your parent is {parent.agent_id}. "
        "Maintain your own identity. Call agent_finish when your task is complete."
    )
    parts.append(engagement_context(services.spec))
    parts.append(task)
    parts.append(
        "Stay strictly within the authorized scope above. File validated findings "
        "with create_vulnerability_report"
        + (" or create_internal_finding for internal discoveries" if _is_internal(services) else "")
        + ". When your assignment is done (or blocked), call agent_finish with a "
        "result summary for your parent."
    )
    return [{"role": "user", "content": "\n\n".join(parts)}]


def _is_internal(services: EngineServices) -> bool:
    spec = services.spec
    return bool(spec is not None and getattr(spec, "scan_type", "") == "internal")
