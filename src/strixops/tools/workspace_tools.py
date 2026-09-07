"""Working-memory tools: think and todo tracking."""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from strixops.engine.scanconfig import EngineContext

_TODO_STATUS = {"pending", "in_progress", "done"}


@function_tool(strict_mode=False)
def think(ctx: RunContextWrapper[EngineContext], thought: str) -> str:
    """Record a private reasoning step before acting.

    Use this to plan: enumerate hypotheses, pick the next probe, weigh
    evidence before committing to a finding. This scratchpad is visible to
    operators reviewing the session, so make reasoning legible.

    Args:
        thought: Your reasoning: what you know, what you're testing next, why.
    """
    return "Recorded. Continue with your next tool call."


@function_tool(strict_mode=False)
def create_todo(ctx: RunContextWrapper[EngineContext], todos: str) -> str:
    """Create a structured work plan of test areas and hypotheses.

    Args:
        todos: JSON array of objects, e.g.
            [{"title": "Map authentication flow", "status": "pending",
              "priority": "high"}]. status: pending|in_progress|done.
    """
    try:
        items = json.loads(todos)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "errors": ["todos must be a JSON array"]})
    if not isinstance(items, list):
        return json.dumps({"success": False, "errors": ["todos must be a JSON array"]})

    state = ctx.context
    state.todos = []
    for item in items:
        if not isinstance(item, dict) or "title" not in item:
            continue
        entry = {
            "title": str(item.get("title", "")),
            "status": str(item.get("status", "pending")),
            "priority": str(item.get("priority", "medium")),
        }
        if entry["status"] not in _TODO_STATUS:
            entry["status"] = "pending"
        state.todos.append(entry)
    return json.dumps({"success": True, "message": f"{len(state.todos)} todo item(s) created."})


@function_tool(strict_mode=False)
def update_todo(ctx: RunContextWrapper[EngineContext], updates: str) -> str:
    """Update statuses/priorities of existing todo items.

    Args:
        updates: JSON array, e.g. [{"title": "Map auth flow", "status": "done"}].
    """
    try:
        items = json.loads(updates)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "errors": ["updates must be a JSON array"]})
    if not isinstance(items, list):
        return json.dumps({"success": False, "errors": ["updates must be a JSON array"]})

    todos = ctx.context.todos
    applied = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", ""))
        for entry in todos:
            if entry["title"] == title:
                if "status" in item and item["status"] in _TODO_STATUS:
                    entry["status"] = str(item["status"])
                if "priority" in item:
                    entry["priority"] = str(item["priority"])
                applied += 1
                break
    return json.dumps({"success": True, "message": f"{applied} todo item(s) updated."})
