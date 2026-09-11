"""Run-shared working notes; failures are recoverable tool results."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from agents import RunContextWrapper, function_tool

from strixops.engine.scanconfig import EngineContext
from strixops.report.notes import error_result


def _tool_failure(ctx: Any, error: Exception) -> str:
    return json.dumps(error_result("invalid_arguments"))


async def _run_in_thread(operation: Any, **kwargs: Any) -> dict:
    worker = asyncio.create_task(asyncio.to_thread(operation, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # A thread cannot be cancelled mid-rename. Join this one bounded file
        # operation so no note writer outlives Agent/run cleanup, then propagate.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with suppress(Exception, asyncio.CancelledError):
            worker.result()
        raise


async def _invoke(
    ctx: RunContextWrapper[EngineContext], operation: str, *, author: bool = False, **kwargs: Any
) -> str:
    try:
        state = ctx.context.run_state
        if state is None:
            return json.dumps(error_result("storage_unavailable"))
        if author:
            kwargs.update(agent_id=ctx.context.agent_id, agent_name=ctx.context.agent_name)
        # File access is bounded but may block; serialize writes under the shared
        # store lock in a worker. Cancellation of the awaiting task propagates.
        result = await _run_in_thread(getattr(state.notes, operation), **kwargs)
    except Exception:
        result = error_result("internal_error")
    return json.dumps(result, ensure_ascii=False)


@function_tool(strict_mode=False, failure_error_function=_tool_failure)
async def create_note(
    ctx: RunContextWrapper[EngineContext],
    title: str,
    content: str,
    category: str = "general",
    tags: list[str] | None = None,
) -> str:
    """Create an attributed working note shared only by this run's agents.

    Categories: general, findings, methodology, questions, plan, wiki.
    Notes do not file findings or replace coverage, todos or messages.
    Limits: title 200 chars, content 32768 chars, 20 tags of 64 chars.
    """
    return await _invoke(
        ctx, "create_note", author=True, title=title, content=content, category=category, tags=tags
    )


@function_tool(strict_mode=False, failure_error_function=_tool_failure)
async def list_notes(
    ctx: RunContextWrapper[EngineContext],
    query: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    author: str | None = None,
    include_deleted: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Search shared note previews; use get_note for complete content.

    Query searches title/content/tags. Tags require all requested tags; author
    matches creator or last editor ID/name. Limit 1–100; deleted notes hidden
    unless include_deleted. Results sort by most recently updated first.
    """
    return await _invoke(
        ctx,
        "list_notes",
        query=query,
        category=category,
        tags=tags,
        author=author,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )


@function_tool(strict_mode=False, failure_error_function=_tool_failure)
async def get_note(ctx: RunContextWrapper[EngineContext], note_id: str, include_history: bool = False) -> str:
    """Read a note's current content/revision, including soft-deleted notes.

    Optional history retains at most 20 prior revisions; history_truncated
    identifies older omitted versions. Authors are derived from agent context.
    """
    return await _invoke(ctx, "get_note", note_id=note_id, include_history=include_history)


@function_tool(strict_mode=False, failure_error_function=_tool_failure)
async def update_note(
    ctx: RunContextWrapper[EngineContext],
    note_id: str,
    expected_revision: int | None = None,
    title: str | None = None,
    content: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    append_content: str | None = None,
) -> str:
    """Replace fields with expected_revision, or atomically append text.

    Append cannot combine with replacement fields; it adds exact text, so
    include your own newline. Append may omit expected_revision. If supplied,
    it must match. On revision_conflict, get_note and merge before retrying.
    """
    return await _invoke(
        ctx,
        "update_note",
        author=True,
        note_id=note_id,
        expected_revision=expected_revision,
        title=title,
        content=content,
        category=category,
        tags=tags,
        append_content=append_content,
    )


@function_tool(strict_mode=False, failure_error_function=_tool_failure)
async def delete_note(ctx: RunContextWrapper[EngineContext], note_id: str, expected_revision: int) -> str:
    """Soft-delete a note only if expected_revision is current.

    Attribution and content remain readable by ID and in deletion history.
    On revision_conflict, read the note before deciding whether to delete it.
    """
    return await _invoke(
        ctx, "delete_note", author=True, note_id=note_id, expected_revision=expected_revision
    )
