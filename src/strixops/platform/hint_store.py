"""Bounded, anchored storage shared by the Console and operator-hint poller."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

MAX_MESSAGE_CHARS = 16_000
MAX_FILE_BYTES = 128 * 1024
MAX_INBOX_BYTES = 16 * 1024 * 1024
MAX_HINTS = 10_000
ACTIVE_AGENT_STATUSES = {"running", "waiting"}
FINAL_HINT_STATUSES = {"delivered", "acked", "failed"}
_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
_SAFE_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\.json\Z")
ERRORS = {
    "invalid_message": (400, "A non-empty instruction is required."),
    "message_too_long": (413, "The instruction exceeds the 16,000-character limit."),
    "invalid_agent_id": (400, "The agent ID is invalid."),
    "invalid_run_name": (400, "The task name is invalid."),
    "invalid_request_id": (400, "The client request ID must be a UUID."),
    "invalid_phase": (400, "The instruction phase is invalid."),
    "unknown_agent": (404, "The selected agent does not exist in this task."),
    "agent_not_active": (409, "The selected agent can no longer receive instructions."),
    "run_not_active": (409, "This task can no longer receive instructions."),
    "idempotency_conflict": (409, "This request ID has already been used for another instruction."),
    "delivery_failed": (503, "The instruction could not be delivered to the selected agent."),
    "storage_unavailable": (503, "Instruction storage is unavailable. Retry with the same request ID."),
    "inbox_limit": (409, "This task has reached the instruction storage limit."),
}


class HintError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if code in ERRORS else "storage_unavailable"
        self.status, self.message = ERRORS[self.code]
        super().__init__(self.message)


def canonical_payload(message: str, agent_id: str = "", phase: str = "1") -> dict[str, str]:
    if not message.strip() or "\x00" in message:
        raise HintError("invalid_message")
    try:
        message.encode("utf-8")
    except UnicodeError:
        raise HintError("invalid_message") from None
    if len(message) > MAX_MESSAGE_CHARS:
        raise HintError("message_too_long")
    agent_id = agent_id.strip() or "root"
    if not _AGENT_ID.fullmatch(agent_id):
        raise HintError("invalid_agent_id")
    phase = phase.strip() or "1"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", phase):
        raise HintError("invalid_phase")
    return {"message": message.strip(), "agent_id": agent_id, "phase": phase}


def validate_run_name(name: str) -> None:
    if not _RUN_NAME.fullmatch(name):
        raise HintError("invalid_run_name")


def canonical_request_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        result = str(uuid.UUID(value))
        if value.lower() != result:
            raise ValueError
        return result
    except (ValueError, AttributeError):
        raise HintError("invalid_request_id") from None


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_closed(directory: int) -> bool:
    try:
        os.stat(".closed", dir_fd=directory, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def close_inbox(directory: int) -> None:
    """A persistent closing marker closes the POST/finalization admission race."""
    if is_closed(directory):
        return
    fd = os.open(".closed", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    os.close(fd)


@contextlib.contextmanager
def inbox_directory(path: Path, *, create: bool = False, locked: bool = False) -> Iterator[int | None]:
    """Anchor both parent and inbox; never follow a run/inbox/lock symlink."""
    parent_fd = directory_fd = lock_fd = None
    try:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        try:
            directory_fd = os.open(
                path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
        except FileNotFoundError:
            if create:
                raise
            yield None
            return
        if locked:
            lock_fd = os.open(
                ".write.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600, dir_fd=directory_fd,
            )
            details = os.fstat(lock_fd)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise HintError("storage_unavailable")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield directory_fd
    except OSError:
        raise HintError("storage_unavailable") from None
    finally:
        for fd in (lock_fd, directory_fd, parent_fd):
            if fd is not None:
                os.close(fd)


def read_hint(directory: int, filename: str) -> dict[str, Any] | None:
    if not _SAFE_FILE.fullmatch(filename):
        raise HintError("storage_unavailable")
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            details = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                or details.st_size > MAX_FILE_BYTES
            ):
                raise HintError("storage_unavailable")
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise HintError("storage_unavailable")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise HintError("storage_unavailable")
        return data
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise HintError("storage_unavailable") from None


def read_entries(directory: int | None) -> list[tuple[str, dict[str, Any]]]:
    if directory is None:
        return []
    names = sorted(name for name in os.listdir(directory) if name.endswith(".json"))
    if len(names) > MAX_HINTS:
        raise HintError("inbox_limit")
    entries = []
    total = 0
    for name in names:
        try:
            hint = read_hint(directory, name)
            size = len(json.dumps(hint, ensure_ascii=False).encode("utf-8")) if hint is not None else 0
        except (HintError, OSError, UnicodeError, ValueError, RecursionError):
            continue  # A damaged/unsafe legacy file cannot block the rest of the inbox.
        if hint is not None:
            total += size
            if total > MAX_INBOX_BYTES:
                raise HintError("inbox_limit")
            entries.append((name, hint))
    return entries


def write_hint(directory: int, filename: str, hint: dict[str, Any]) -> None:
    if not _SAFE_FILE.fullmatch(filename):
        raise HintError("storage_unavailable")
    try:
        raw = json.dumps(hint, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise HintError("storage_unavailable") from None
    if len(raw) > MAX_FILE_BYTES:
        raise HintError("inbox_limit")
    temp = f".hint-{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, filename, src_dir_fd=directory, dst_dir_fd=directory)
    except OSError:
        raise HintError("storage_unavailable") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp, dir_fd=directory)


def project_hint(hint: dict[str, Any], filename: str = "") -> dict[str, Any]:
    status = str(hint.get("status") or "queued")
    if status not in {"queued", *FINAL_HINT_STATUSES}:
        status = "failed"
    result = {
        key: str(hint.get(key) or "")
        for key in ("message_id", "agent_name", "message", "hint_token", "created_at")
    }
    result.update(
        message_id=result["message_id"] or Path(filename).stem,
        agent_id=str(hint.get("agent_id") or "root"), status=status,
    )
    if status == "failed":
        code = str(hint.get("failure_code") or "delivery_failed")
        code = code if code in ERRORS else "delivery_failed"
        result.update(failure_code=code, failure_reason=ERRORS[code][1])
    for key in ("delivered_at", "failed_at"):
        if isinstance(hint.get(key), str):
            result[key] = hint[key]
    return result
