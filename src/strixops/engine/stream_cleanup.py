"""Join SDK stream writers before their SQLite session or sandbox is closed."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


def stream_tasks(stream: Any) -> list[asyncio.Task[Any]]:
    return [
        task
        for name in ("run_loop_task", "_input_guardrails_task", "_output_guardrails_task")
        if isinstance(task := getattr(stream, name, None), asyncio.Task)
    ]


def cancel_stream(stream: Any) -> None:
    """Do not interrupt a producer that is already running its async finally."""
    tasks = stream_tasks(stream)
    if not tasks or all(task.done() for task in tasks):
        # Test doubles and SDK results without a producer still need their
        # cancellation sentinel to unblock the consumer.
        if not tasks and callable(getattr(stream, "cancel", None)):
            stream.cancel("immediate")
        return
    if not any(task.cancelling() for task in tasks):
        stream.cancel("immediate")
    else:
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()


async def join_task(task: asyncio.Task[Any]) -> bool:
    """Return whether the caller was cancelled while preserving task cleanup."""
    interrupted = False
    while True:
        try:
            await asyncio.shield(task)
            return interrupted
        except asyncio.CancelledError:
            if task.done():
                if not task.cancelled():
                    task.result()
                    interrupted = True
                return interrupted
            interrupted = True


async def settle_stream(stream: Any, *, cancel: bool = False) -> None:
    if cancel:
        cancel_stream(stream)

    async def settle() -> None:
        tasks = stream_tasks(stream)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        cleanup = getattr(stream, "_run_sandbox_cleanup", None)
        if callable(cleanup):
            await cleanup()

    interrupted = await join_task(asyncio.create_task(settle(), name="strixops-stream-cleanup"))
    if interrupted:
        raise asyncio.CancelledError


async def consume_stream(stream: Any, on_event: Callable[[Any], None]) -> None:
    """Shield the SDK event consumer and finish its producer on cancellation.

    SDK stream_events cancels its producer without awaiting it when its own
    consumer is cancelled. Keeping that consumer alive lets it execute the
    SDK's normal join and sandbox cleanup path, including on operator hints.
    """

    async def consume() -> None:
        async for event in stream.stream_events():
            on_event(event)

    consumer = asyncio.create_task(consume(), name="strixops-stream-events")
    interrupted = False
    try:
        await asyncio.shield(consumer)
    except asyncio.CancelledError:
        interrupted = True
        cancel_stream(stream)
        interrupted = await join_task(consumer) or interrupted
    except BaseException:
        cancel_stream(stream)
        raise
    finally:
        try:
            await settle_stream(stream, cancel=interrupted)
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError
