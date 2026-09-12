"""Read-only maintenance checks; importing this module never starts StrixOps.

Only aggregate counts and fixed labels leave this module. Database paths, run
names, target URLs, job payloads and exception messages are deliberately absent
from diagnostics. These checks are conservative snapshots, not admission locks.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from collections import Counter
from pathlib import Path
from typing import Any

_JSON_LIMIT = 32 * 1024 * 1024
_HEARTBEAT_SECONDS = 300
_QUEUE_ACTIVE = frozenset({"queued", "starting", "waiting_capacity", "running", "cancelling", "blocked"})
_QUEUE_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_LEASE_ACTIVE = frozenset({"waiting", "active", "draining", "blocked"})
_MCP_JOB_ACTIVE = frozenset({"queued", "starting", "running", "cancelling"})
_MCP_JOB_TERMINAL = frozenset({"completed", "failed", "cancelled", "blocked"})
_MCP_TASK_ACTIVE = frozenset({"starting", "capturing", "stopping", "ending", "deleting", "delete_failed"})
_MCP_TASK_IDLE = frozenset({"idle", "stopped", "ended", "error"})
_MCP_SESSION_STATES = frozenset({"starting", "running", "stopping", "error", "stopped"})
_RUN_TERMINAL = frozenset({"completed", "failed", "interrupted", "cancelled"})


class _UnknownActivity(Exception):
    pass


def _unknown(label: str) -> str:
    return f"{label}: activity is unknown; stored state is unreadable or invalid."


def _present(path: Path, *, directory: bool = False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise _UnknownActivity from None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise _UnknownActivity
    return True


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, (str, bytes)) or len(value) > _JSON_LIMIT:
        raise _UnknownActivity
    try:
        result = json.loads(value)
    except (ValueError, RecursionError):
        raise _UnknownActivity from None
    if not isinstance(result, dict):
        raise _UnknownActivity
    return result


def _database(path: Path) -> sqlite3.Connection:
    # mode=ro must observe live WAL contents; immutable=1 can miss pending work.
    # Do not instantiate the runtime stores, which create/migrate/recover state.
    for suffix in ("-wal", "-shm", "-journal"):
        _present(Path(str(path) + suffix))
    connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def _counts(db: sqlite3.Connection, table: str, field: str, known: frozenset[str]) -> Counter[str]:
    # All SQL identifiers come from fixed call sites below, never caller data.
    counts: Counter[str] = Counter()
    for status, count in db.execute(f"SELECT {field}, count(*) FROM {table} GROUP BY {field}"):
        if not isinstance(status, str) or status not in known:
            raise _UnknownActivity
        counts[status] = count
    return counts


def _json_counts(db: sqlite3.Connection, table: str, known: frozenset[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for (raw,) in db.execute(f"SELECT data FROM {table}"):
        status = _json_object(raw).get("status")
        if not isinstance(status, str) or status not in known:
            raise _UnknownActivity
        counts[status] += 1
    return counts


def _summarize(label: str, counts: Counter[str], active: frozenset[str]) -> list[str]:
    values = [(status, count) for status, count in sorted(counts.items()) if status in active and count]
    if not values:
        return []
    details = ", ".join(f"{status}={count}" for status, count in values)
    return [f"{label}: {sum(count for _, count in values)} outstanding ({details})."]


def _queue(path: Path) -> list[str]:
    if not _present(path):
        return []
    db = _database(path)
    try:
        items = _counts(db, "items", "status", _QUEUE_ACTIVE | _QUEUE_TERMINAL)
        leases = _counts(db, "leases", "state", _LEASE_ACTIVE | {"released"})
        return _summarize("Task queue targets", items, _QUEUE_ACTIVE) + _summarize(
            "Task queue leases", leases, _LEASE_ACTIVE
        )
    finally:
        db.close()


def _mcp(root: Path) -> list[str]:
    if not _present(root, directory=True):
        return []
    path = root / "traffic.sqlite3"
    if not _present(path):
        return []
    db = _database(path)
    try:
        jobs = _json_counts(db, "jobs", _MCP_JOB_ACTIVE | _MCP_JOB_TERMINAL)
        tasks = _json_counts(db, "tasks", _MCP_TASK_ACTIVE | _MCP_TASK_IDLE)
        sessions = _json_counts(db, "sessions", _MCP_SESSION_STATES)
        return (
            _summarize("MCP request tests", jobs, _MCP_JOB_ACTIVE)
            + _summarize("MCP tasks", tasks, _MCP_TASK_ACTIVE)
            + _summarize("MCP capture sessions", sessions, _MCP_SESSION_STATES - {"stopped"})
        )
    finally:
        db.close()


def _fofa(root: Path) -> list[str]:
    if not _present(root, directory=True):
        return []
    path = root / "history.sqlite3"
    if not _present(path):
        return []
    db = _database(path)
    try:
        active = frozenset({"queued", "running"})
        counts = _counts(db, "searches", "status", active | {"completed", "partial", "failed"})
        return _summarize("FOFA searches", counts, active)
    finally:
        db.close()


def _read_file(path: Path, limit: int = _JSON_LIMIT) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise _UnknownActivity
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise _UnknownActivity
    return data


def _run_pid_alive(directory: Path) -> bool | None:
    path = directory / ".console.pid"
    if not _present(path):
        return None
    try:
        raw = _read_file(path, 64).strip()
        if not raw.isdigit() or int(raw) <= 0:
            return None
        os.kill(int(raw), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, OverflowError):
        return None


def _recent_heartbeat(directory: Path) -> bool:
    path = directory / "events.jsonl"
    if not _present(path):
        return False
    modified = path.stat().st_mtime
    return modified > 0 and time.time() - modified < _HEARTBEAT_SECONDS


def _runs(root: Path) -> list[str]:
    if not _present(root, directory=True):
        return []
    live = unknown = 0
    for directory in root.iterdir():
        if directory.name.startswith("."):
            continue
        try:
            if not _present(directory, directory=True):
                continue
        except _UnknownActivity:
            # Normal files beside run directories are not run metadata.
            if directory.is_file() and not directory.is_symlink():
                continue
            unknown += 1
            continue
        try:
            record_path = directory / "run.json"
            if _present(record_path):
                record = _json_object(_read_file(record_path))
                status = record.get("status")
                if status in _RUN_TERMINAL:
                    # Historical terminal records often retain a recycled PID.
                    # Explicit in-progress cleanup is still unsafe to interrupt.
                    cleanup = record.get("cleanup")
                    if isinstance(cleanup, dict) and cleanup.get("status") == "in_progress":
                        unknown += 1
                    continue
                if status != "running":
                    unknown += 1
                    continue
            else:
                if not any(
                    _present(directory / name)
                    for name in (".console.pid", "events.jsonl", ".console_launch.json")
                ):
                    continue
            pid_alive = _run_pid_alive(directory)
            if pid_alive is True or _recent_heartbeat(directory):
                live += 1
            elif pid_alive is None:
                unknown += 1
        except (OSError, ValueError, TypeError, _UnknownActivity):
            unknown += 1
    result = []
    if live:
        result.append(f"Scan runs: {live} live or still starting.")
    if unknown:
        result.append(f"Scan runs: {unknown} record(s) have unknown activity; verify task state first.")
    return result


def activity_blockers(paths: dict[str, str], *, health: dict | None = None) -> list[str]:
    """Return aggregate reasons to postpone maintenance, without mutating stores.

    ``paths`` requires runs_root, queue_db, mcp_root, and fofa_root. Missing stores
    mean unused features. Existing unreadable/unknown state fails conservatively.
    ``health`` is an optional already-fetched /api/health response; this helper
    performs no network requests and never signals a process.
    """
    result: list[str] = []
    if health is not None:
        live = health.get("live_runs") if isinstance(health, dict) else None
        if type(live) is not int or live < 0:
            result.append("Console health: live task activity is unknown.")
        elif live:
            result.append(f"Console health: {live} live scan run(s).")
    for key, label, check in (
        ("runs_root", "Scan runs", _runs),
        ("queue_db", "Task queue", _queue),
        ("mcp_root", "MCP tasks", _mcp),
        ("fofa_root", "FOFA searches", _fofa),
    ):
        try:
            raw = paths.get(key)
            if not isinstance(raw, str) or not raw.strip():
                raise _UnknownActivity
            result.extend(check(Path(raw).expanduser()))
        except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error, _UnknownActivity):
            result.append(_unknown(label))
    return result
