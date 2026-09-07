"""SDK conversation sessions, following Strix's connection and rewrite behavior."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from weakref import WeakKeyDictionary

from agents.items import ItemHelpers, TResponseInputItem
from agents.memory import Session, SQLiteSession

logger = logging.getLogger(__name__)
_session_write_locks: WeakKeyDictionary[Session, asyncio.Lock] = WeakKeyDictionary()


class _PooledConnectionSession(SQLiteSession):
    @contextmanager
    def _locked_connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise RuntimeError("SQLiteSession is closed")
            if self._is_memory_db:
                yield self._shared_connection
                return
            connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
            try:
                yield connection
            finally:
                connection.close()


def open_agent_session(agent_id: str, path: Path) -> SQLiteSession:
    path.parent.mkdir(parents=True, exist_ok=True)
    return _PooledConnectionSession(session_id=agent_id, db_path=path)


def session_write_lock(session: Session) -> asyncio.Lock:
    """Serialize out-of-band seed, message and compaction writes per session."""
    lock = _session_write_locks.get(session)
    if lock is None:
        lock = asyncio.Lock()
        _session_write_locks[session] = lock
    return lock


async def seed_initial_input(session: Session, initial_input: Any) -> bool:
    """Persist opening input once, before the first SDK run can fail."""
    items = ItemHelpers.input_to_new_input_list(initial_input)
    if not items:
        return False
    async with session_write_lock(session):
        if await session.get_items():
            return False
        await session.add_items(items)
    return True


async def replace_session_items(
    session: Session,
    new_items: list[Any],
    *,
    expected_len: int | None = None,
) -> bool:
    """Rewrite a session, skipping stale summaries and restoring failed writes."""
    async with session_write_lock(session):
        original = list(await session.get_items())
        if expected_len is not None and len(original) != expected_len:
            logger.warning(
                "skipping session rewrite: expected %d items, found %d", expected_len, len(original)
            )
            return False
        rebuilt = cast(list[TResponseInputItem], new_items)
        await session.clear_session()
        try:
            await session.add_items(rebuilt)
        except Exception:
            logger.exception("session rewrite failed; restoring original items")
            await session.clear_session()
            await session.add_items(original)
            raise
        return True


_IMAGE_REJECTED_TEXT = "[image rejected by the model]"
_IMAGE_ELIDED_TEXT = "[older screenshot elided to bound context memory]"
_INHERITED_IMAGE_TEXT = "[screenshot omitted from inherited context]"


def _output_has_image(item: dict[str, Any]) -> bool:
    return (
        item.get("type") == "function_call_output"
        and isinstance(item.get("output"), list)
        and any(isinstance(block, dict) and block.get("type") == "input_image" for block in item["output"])
    )


def _elided_output(item: dict[str, Any], text: str) -> dict[str, Any]:
    # Keep sibling text blocks and the tool call's identity intact.
    output = item.get("output")
    blocks = output if isinstance(output, list) else []
    return {
        "type": "function_call_output",
        "call_id": item.get("call_id"),
        "output": [
            {"type": "input_text", "text": text}
            if isinstance(block, dict) and block.get("type") == "input_image"
            else block
            for block in blocks
        ],
    }


async def _rewrite_session(
    session: Session,
    transform: Callable[[list[Any]], tuple[list[Any], bool]],
) -> bool:
    """Apply an image-history change under the reference's session write lock."""
    async with session_write_lock(session):
        original = list(await session.get_items())
        if not original:
            return False
        rebuilt, changed = transform(original)
        if not changed:
            return False
        await session.clear_session()
        try:
            await session.add_items(cast(list[TResponseInputItem], rebuilt))
        except Exception:
            logger.exception("session rewrite failed; restoring original items")
            await session.clear_session()
            await session.add_items(original)
            raise
        return True


async def strip_all_images_from_session(session: Session) -> bool:
    """Replace image tool-output blocks with text after model input rejection."""

    def transform(items: list[Any]) -> tuple[list[Any], bool]:
        rebuilt: list[Any] = []
        changed = False
        for item in items:
            if isinstance(item, dict) and _output_has_image(item):
                rebuilt.append(_elided_output(item, _IMAGE_REJECTED_TEXT))
                changed = True
            else:
                rebuilt.append(item)
        return rebuilt, changed

    return await _rewrite_session(session, transform)


async def enforce_image_budget(session: Session, max_images: int) -> bool:
    """Keep the newest image outputs, replacing older image blocks with text."""
    if max_images < 0:
        return False

    def transform(items: list[Any]) -> tuple[list[Any], bool]:
        image_indices = [
            index for index, item in enumerate(items) if isinstance(item, dict) and _output_has_image(item)
        ]
        if len(image_indices) <= max_images:
            return items, False
        to_elide = set(image_indices[: len(image_indices) - max_images])
        return [
            _elided_output(item, _IMAGE_ELIDED_TEXT) if index in to_elide else item
            for index, item in enumerate(items)
        ], True

    return await _rewrite_session(session, transform)


def scrub_images_from_items(items: list[Any]) -> list[Any]:
    """Copy parent history with image blocks replaced by the original marker."""

    def scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            if obj.get("type") == "input_image":
                return {"type": "input_text", "text": _INHERITED_IMAGE_TEXT}
            return {key: scrub(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [scrub(value) for value in obj]
        return obj

    return [scrub(item) for item in items]
