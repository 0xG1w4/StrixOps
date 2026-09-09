"""Multi-agent collaboration tools.

Shapes (platform parser): ``create_agent`` carries ``name``+``task``
(+``skills``) which the platform renders as a dispatch event;
``wait_for_agents``/``send_message_to_agent``/``stop_agent``/
``view_agent_graph`` must stay free of classifier keywords — see
``tests/contract/test_tool_shapes.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agents import RunContextWrapper, function_tool

from strixops.engine.coordinator import TERMINAL_STATUSES, AgentCoordinator
from strixops.engine.scanconfig import EngineContext, EngineServices

WAIT_POLL_SECONDS = 0.3
DEFAULT_WAIT_TIMEOUT = 600.0


@function_tool(strict_mode=False)
async def create_agent(
    ctx: RunContextWrapper[EngineContext],
    name: str,
    task: str,
    inherit_context: bool = True,
    skills: list[str] | None = None,
) -> str:
    """Spawn a specialized child agent to execute a testing assignment.

    Delegate a bounded subtask when it helps your assignment. Give each child
    a crisp, self-contained scope, goal and reporting requirement. Children
    run concurrently and report back when done; each agent remains responsible
    for its own assignment. Consult view_agent_graph for configured depth,
    active and lifetime limits. A rejected spawn creates no child.

    Args:
        name: Short descriptive agent name, e.g. "auth-bypass-prober".
        task: The complete assignment: scope, goal, method hints, and what
            to file before finishing.
        inherit_context: Give the child this turn's input history as background.
            Set False for a task that should start without inherited history.
        skills: Skill names (e.g. "vulnerabilities/sql_injection") whose full
            playbook content is INJECTED into the child's system prompt before
            it starts — pick the discipline that matches the assignment.
    """
    services: EngineServices = ctx.context.services  # type: ignore[assignment]
    parent_history = list(ctx.turn_input) if inherit_context and ctx.turn_input else []
    result = services.spawn_child(  # type: ignore[misc]
        name=name.strip(),
        task=task,
        parent_history=parent_history,
        skills=list(skills or []),
        parent=ctx.context,
    )
    if not result.get("ok"):
        return json.dumps(
            {
                "success": False,
                "message": result.get("error", "spawn failed"),
                "error_code": result.get("error_code", "SPAWN_FAILED"),
            }
        )
    return json.dumps(
        {
            "success": True,
            "message": f"Agent {result['name']} ({result['agent_id']}) spawned.",
            "agent_id": result["agent_id"],
            "name": result["name"],
        }
    )


@function_tool(strict_mode=False)
async def wait_for_agents(
    ctx: RunContextWrapper[EngineContext],
    reason: str,
    timeout_seconds: int = 600,
) -> str:
    """Wait for your child agents to finish, collecting their reports.

    Blocks until every child has reached a terminal state (or a message
    arrives for you, or the timeout elapses). Children deliver completion
    reports here.

    Args:
        reason: Why you are waiting (e.g. "scanner probing auth flows").
        timeout_seconds: Maximum time to wait.
    """
    services: EngineServices = ctx.context.services  # type: ignore[assignment]
    coordinator: AgentCoordinator = services.coordinator  # type: ignore[assignment]
    me = ctx.context.agent_id

    children = coordinator.descendants_of(me)
    if not children:
        pending = coordinator.drain_messages(me)
        return json.dumps(
            {
                "success": True,
                "message": "No child agents to wait for.",
                "messages": [m.get("content", "") for m in pending],
            }
        )

    await coordinator.set_status(me, "waiting")
    try:
        deadline = asyncio.get_running_loop().time() + max(1, int(timeout_seconds))
        timed_out = False
        while True:
            outstanding = coordinator.active_descendants(me)
            if not outstanding:
                break
            if coordinator.pending_messages(me):
                break
            if asyncio.get_running_loop().time() >= deadline:
                timed_out = True
                break
            await asyncio.sleep(WAIT_POLL_SECONDS)
    finally:
        # A hint can interrupt the wait; terminal agents must never be revived.
        entry = coordinator.entry_of(me)
        if entry is not None and entry["status"] not in TERMINAL_STATUSES:
            await coordinator.set_status(me, "running")

    entries = {
        coordinator.name_of(c): (coordinator.entry_of(c) or {}).get("status", "unknown")
        for c in coordinator.children_of(me)
    }
    messages = [m.get("content", "") for m in coordinator.drain_messages(me)]
    outstanding = coordinator.active_descendants(me)

    summary = "; ".join(f"{name}={status}" for name, status in entries.items())
    message = (
        f"Timed out after {timeout_seconds}s — children still working."
        if timed_out
        else f"Message received; {len(outstanding)} descendant(s) are still working or settling."
        if outstanding
        else f"Children finished: {summary}"
        if entries
        else "No child agents found."
    )
    return json.dumps(
        {
            "success": True,
            "message": message,
            "timed_out": timed_out,
            "active_agent_ids": outstanding,
            "children": entries,
            "messages": messages,
        }
    )


@function_tool(strict_mode=False)
async def send_message_to_agent(
    ctx: RunContextWrapper[EngineContext],
    target_agent_id: str,
    message: str,
    message_type: str = "info",
    priority: str = "normal",
) -> str:
    """Send a message to another agent (e.g. steer a child mid-task).

    Args:
        target_agent_id: The agent id from create_agent's response.
        message: What you want to tell them.
        message_type: info | instruction | warning.
        priority: normal | high.
    """
    services: EngineServices = ctx.context.services  # type: ignore[assignment]
    coordinator: AgentCoordinator = services.coordinator  # type: ignore[assignment]
    sent = await coordinator.send(
        target_agent_id,
        {
            "from": ctx.context.agent_id,
            "from_name": ctx.context.agent_name,
            "type": str(message_type),
            "priority": str(priority),
            "content": str(message),
        },
    )
    return json.dumps(
        {
            "success": sent,
            "message": "Message sent."
            if sent
            else f"Agent {target_agent_id!r} is unknown, finished, or stopping; no message was delivered.",
        }
    )


@function_tool(strict_mode=False)
def view_agent_graph(ctx: RunContextWrapper[EngineContext]) -> str:
    """Show every agent: name, status, and parent-child relationships."""
    services: EngineServices = ctx.context.services  # type: ignore[assignment]
    coordinator: AgentCoordinator = services.coordinator  # type: ignore[assignment]
    graph: dict[str, Any] = {}
    for agent_id in coordinator.agent_ids():
        entry = coordinator.entry_of(agent_id) or {}
        graph[agent_id] = {
            "name": entry.get("name", agent_id),
            "status": entry.get("status", "unknown"),
            "parent": entry.get("parent_id"),
            "task": entry.get("task", ""),
            "depth": entry.get("depth", 0),
        }
    return json.dumps(
        {
            "success": True,
            "agents": graph,
            "limits": {
                "max_depth": coordinator.limits.max_depth,
                "max_active": coordinator.limits.max_active,
                "max_total": coordinator.limits.max_total,
                "root_depth": 0,
                "root_counts_toward_limits": True,
            },
        }
    )


@function_tool(strict_mode=False)
async def stop_agent(
    ctx: RunContextWrapper[EngineContext],
    target_agent_id: str,
    cascade: bool = True,
    reason: str = "",
) -> str:
    """Stop an agent that is stuck, looping, or no longer needed.

    Args:
        target_agent_id: The agent to stop.
        cascade: Also stop its descendants.
        reason: Why it is being stopped (recorded for the operator).
    """
    services: EngineServices = ctx.context.services  # type: ignore[assignment]
    coordinator: AgentCoordinator = services.coordinator  # type: ignore[assignment]

    if target_agent_id not in coordinator.descendants_of(ctx.context.agent_id):
        return json.dumps({"success": False, "message": "You may stop only your own descendants."})
    if not cascade and coordinator.active_descendants(target_agent_id):
        return json.dumps(
            {
                "success": False,
                "message": "This agent has active descendants; use cascade=true to stop the whole branch.",
            }
        )
    targets = [target_agent_id]
    if cascade:
        frontier = [target_agent_id]
        while frontier:
            current = frontier.pop()
            for child in coordinator.children_of(current):
                targets.append(child)
                frontier.append(child)

    stopped: list[str] = []
    for agent_id in dict.fromkeys(targets):
        if await coordinator.cancel_agent(agent_id):
            stopped.append(coordinator.name_of(agent_id))
    return json.dumps(
        {
            "success": True,
            "message": f"Stopped: {', '.join(stopped)}" if stopped else "Nothing to stop (already terminal).",
            "stopped": stopped,
        }
    )
