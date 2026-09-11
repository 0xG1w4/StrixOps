"""Private independent FOFA settings; corrupt existing data is never reset."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path

from .errors import FofaError


def default_root() -> Path:
    override = (os.environ.get("STRIXOPS_FOFA_ROOT") or "").strip()
    if override:
        return Path(override).expanduser()
    from strixops.console.settings_store import config_path

    return config_path().parent / "fofa"


def checked_root(root: Path, *, create: bool = False) -> bool:
    try:
        if create:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise FofaError("storage_unavailable", 503)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise FofaError("storage_unavailable", 503) from None


def checked_file(path: Path, *, missing: bool = False) -> bool:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise FofaError("storage_unavailable", 503)
        return True
    except FileNotFoundError:
        if missing:
            return False
        raise FofaError("storage_unavailable", 503) from None
    except OSError:
        raise FofaError("storage_unavailable", 503) from None


@contextmanager
def lock(root: Path, name: str, *, nonblocking: bool = False):
    checked_root(root, create=True)
    descriptor = None
    try:
        descriptor = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise FofaError("storage_unavailable", 503)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError:
            raise FofaError("search_running", 409) from None
        yield
    except OSError:
        raise FofaError("storage_unavailable", 503) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _valid_text(value: object, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise FofaError("invalid_request")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise FofaError("invalid_request") from None
    return value.strip()


def read_settings(root: Path) -> dict:
    defaults = {"enabled": True, "email": "", "key": ""}
    if not checked_root(root) or not checked_file(root / "settings.json", missing=True):
        return defaults
    try:
        fd = os.open(root / "settings.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384:
                raise FofaError("storage_unavailable", 503)
            raw = stream.read(16385)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1 or type(data.get("enabled")) is not bool:
            raise ValueError
        return {
            "enabled": data["enabled"],
            "email": _valid_text(data.get("email"), 320),
            "key": _valid_text(data.get("key"), 8192),
        }
    except (OSError, ValueError, RecursionError, FofaError):
        raise FofaError("storage_unavailable", 503) from None


def public_settings(data: dict) -> dict:
    return {
        "enabled": data["enabled"],
        "email": data["email"],
        "key_set": bool(data["key"]),
        "key_masked": "••••••••" if data["key"] else "",
    }


def save_settings(root: Path, changes: dict) -> dict:
    if set(changes) - {"enabled", "email", "key", "clear_key"}:
        raise FofaError("invalid_request")
    for field in ("enabled", "clear_key"):
        if field in changes and type(changes[field]) is not bool:
            raise FofaError("invalid_request")
    email = _valid_text(changes["email"], 320) if "email" in changes else None
    key = _valid_text(changes["key"], 8192) if "key" in changes else None
    if changes.get("clear_key") and key and not key.startswith(("•", "*")):
        raise FofaError("invalid_request")
    temporary = None
    with lock(root, ".settings.lock"):
        data = read_settings(root)
        if email is not None:
            data["email"] = email
        if key and not key.startswith(("•", "*")):
            data["key"] = key
        if changes.get("clear_key"):
            data["key"] = ""
        if "enabled" in changes:
            data["enabled"] = changes["enabled"]
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".settings-", dir=root)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"version": 1, **data}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, root / "settings.json")
            temporary = None
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            raise FofaError("storage_unavailable", 503) from None
        finally:
            if temporary:
                with suppress(OSError):
                    Path(temporary).unlink()
    return public_settings(data)
