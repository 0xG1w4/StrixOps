"""Private JSON object stores with process-safe read/modify/write transactions."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


class StoreError(RuntimeError):
    """A safe storage error without paths, document contents, or credentials."""

    def __init__(self) -> None:
        super().__init__("Console storage is unavailable or invalid.")


def revision(value: Any) -> int:
    """Legacy records have revision zero; booleans are not revision numbers."""
    return value if type(value) is int and value >= 0 else 0


def _path(path: Path | str) -> Path:
    try:
        result = Path(path).expanduser().absolute()
        if not result.name:
            raise StoreError()
        return result
    except (OSError, RuntimeError, ValueError):
        raise StoreError() from None


def _regular(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StoreError()


def _read(directory: int, name: str) -> dict[str, Any]:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise StoreError() from None
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            _regular(os.fstat(stream.fileno()))

            def reject_constant(_value: str) -> None:
                raise ValueError("invalid JSON number")

            value = json.load(stream, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise StoreError()
        return value
    except (OSError, ValueError, RecursionError):
        raise StoreError() from None


def read(path: Path | str) -> dict[str, Any]:
    """Read without creating files; only an absent store is an empty object."""
    path = _path(path)
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise StoreError() from None
    try:
        return _read(directory, path.name)
    finally:
        os.close(directory)


@contextmanager
def _locked(path: Path) -> Iterator[int]:
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(path), threading.Lock())
    with lock:
        directory = descriptor = None
        try:
            try:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                name = path.name + ".lock"
                descriptor = os.open(
                    name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
                    dir_fd=directory,
                )
                info = os.fstat(descriptor)
                _regular(info)
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise StoreError()
            except (OSError, ValueError):
                raise StoreError() from None
            yield directory
        finally:
            # Closing releases flock even if a caller aborts the transaction.
            for opened in (descriptor, directory):
                if opened is not None:
                    with suppress(OSError):
                        os.close(opened)


def _write(directory: int, name: str, value: dict[str, Any]) -> None:
    temporary = ""
    try:
        encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        # A non-cooperating writer must not redirect an otherwise valid transaction.
        with suppress(FileNotFoundError):
            _regular(os.stat(name, dir_fd=directory, follow_symlinks=False))
        temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
        descriptor = os.open(
            temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory,
        )
        temporary = temporary_name
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = ""
        os.fsync(directory)
    except (OSError, ValueError, TypeError, RecursionError):
        raise StoreError() from None
    finally:
        if temporary:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory)


@contextmanager
def transaction(path: Path | str) -> Iterator[dict[str, Any]]:
    """Hold both locks through reading, caller edits, and atomic publication.

    Exceptions from the caller abort without saving. Do not open another
    transaction for this same store while inside its transaction context.
    """
    path = _path(path)
    with _locked(path) as directory:
        value = _read(directory, path.name)
        yield value
        _write(directory, path.name, value)
