"""Read-only Console projections of the engine-owned, per-run notes file."""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from strixops.report import notes as core

OpenRunFile = Callable[[Path, str], int]
_STORAGE_ERRORS = {"storage_unavailable", "invalid_store", "unsafe_storage", "store_limit"}


def _read_document(run_dir: Path, open_file: OpenRunFile) -> tuple[dict, str]:
    """Read through the Console's anchored opener without creating a store."""
    try:
        fd = open_file(run_dir, ".state/notes.json")
        with os.fdopen(fd, "rb") as handle:
            details = os.fstat(handle.fileno())
            if details.st_nlink != 1:
                raise core.NotesError("unsafe_storage")
            if details.st_size > core.MAX_STORE_BYTES:
                raise core.NotesError("store_limit")
            raw = handle.read(core.MAX_STORE_BYTES + 1)
        if len(raw) > core.MAX_STORE_BYTES:
            raise core.NotesError("store_limit")
        return core.parse_document(raw), "available"
    except FileNotFoundError:
        return core.empty_document(), "missing"
    except OSError as exc:
        code = "unsafe_storage" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "storage_unavailable"
        raise core.NotesError(code) from None


def list_response(
    run_dir: Path,
    open_file: OpenRunFile,
    *,
    category: str | None = None,
    tags: list[str] | None = None,
    search: str | None = None,
    author: str | None = None,
    include_deleted: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> tuple[dict, int]:
    source = "unreadable"
    try:
        document, source = _read_document(run_dir, open_file)
        result = core.project_list(
            document,
            query=search,
            category=category,
            tags=tags,
            author=author,
            include_deleted=include_deleted,
            limit=limit,
            offset=offset,
        )
        return {**result, "source_status": source}, 200
    except core.NotesError as exc:
        unavailable = exc.code in _STORAGE_ERRORS
        return {
            **core.error_result(exc.code),
            "source_status": "unreadable" if unavailable else source,
            "notes": [],
            "total": None,
            "limit": limit,
            "offset": offset,
            "has_more": False,
        }, 200 if unavailable else 422


def detail_response(
    run_dir: Path,
    open_file: OpenRunFile,
    note_id: str,
    *,
    include_history: bool = False,
) -> tuple[dict[str, Any], int]:
    source = "unreadable"
    try:
        document, source = _read_document(run_dir, open_file)
        result = core.project_get(document, note_id, include_history=include_history)
        return {**result, "source_status": source}, 200
    except core.NotesError as exc:
        unavailable = exc.code in _STORAGE_ERRORS
        status = 503 if unavailable else 404 if exc.code == "note_not_found" else 422
        return {
            **core.error_result(exc.code),
            "source_status": "unreadable" if unavailable else source,
            "note": None,
        }, status
