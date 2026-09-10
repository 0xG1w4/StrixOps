"""Persistent automatic Console credentials, independent from individual MCP tasks."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import tempfile
import threading
from contextlib import suppress
from pathlib import Path


class AccessCredentialsError(RuntimeError):
    """Credential storage requires repair; existing credentials are never replaced."""


_LOCK = threading.RLock()
_INVALID = "Saved MCP access credentials are invalid; repair or restore the MCP credential store"
_UNAVAILABLE = "Cannot access MCP credentials; check the Console user's MCP data-directory permissions"


def read_token(root: Path) -> str | None:
    """Read an existing token without creating directories, locks, or task storage."""
    try:
        descriptor = os.open(Path(root) / "access.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AccessCredentialsError(_UNAVAILABLE) from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or stat.S_IMODE(info.st_mode) != 0o600:
                raise AccessCredentialsError(_INVALID)
            data = json.loads(stream.read(4097))
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data.get("version") != 1
            or not isinstance(data.get("control_token"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{64}", data["control_token"])
        ):
            raise AccessCredentialsError(_INVALID)
        return data["control_token"]
    except AccessCredentialsError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AccessCredentialsError(_INVALID) from exc
    except OSError as exc:
        raise AccessCredentialsError(_UNAVAILABLE) from exc


def ensure_token(root: Path) -> str:
    """Generate once under a process and file lock, then publish a private atomic file."""
    root = Path(root)
    temporary: str | None = None
    try:
        with _LOCK:
            if root.is_symlink():
                raise AccessCredentialsError(_INVALID)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(root / ".access.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "r+b") as lock:
                if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                    raise AccessCredentialsError(_INVALID)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                if existing := read_token(root):
                    return existing
                token = secrets.token_urlsafe(48)
                descriptor, temporary = tempfile.mkstemp(prefix=".access-", suffix=".tmp", dir=root)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump({"version": 1, "control_token": token}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, root / "access.json")
                temporary = None
                directory_fd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                return token
    except AccessCredentialsError:
        raise
    except OSError as exc:
        raise AccessCredentialsError(_UNAVAILABLE) from exc
    finally:
        if temporary is not None:
            with suppress(OSError):
                Path(temporary).unlink(missing_ok=True)
