"""Stop verified processes belonging to one checkout, without importing the engine.

PID files are hints, never authority to signal a process. Queue birth identities,
exact checkout entry points, and run arguments establish ownership. Descendants
are captured while their parent is still identifiable, then signalled by PID and
birth identity individually; no process group or name-wide kill is used.
"""

from __future__ import annotations

import ctypes
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_LIMIT = 1024 * 1024


def _table() -> dict[int, tuple[int, int]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,uid="],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode:
        raise OSError("process inventory unavailable")
    rows = {}
    for line in result.stdout.splitlines():
        pid, parent, uid = (int(value) for value in line.split())
        rows[pid] = (parent, uid)
    return rows


def _argv(pid: int) -> list[str]:
    if sys.platform == "darwin":
        # Unlike ps command=, KERN_PROCARGS2 preserves argument boundaries,
        # including checkout paths containing spaces. Never read the environment.
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not 4 < size.value <= _LIMIT:
            raise OSError("process arguments unavailable")
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
            raise OSError("process arguments unavailable")
        data = buffer.raw[: size.value]
        argc = int.from_bytes(data[:4], sys.byteorder, signed=True)
        if not 0 < argc <= 65536:
            raise ValueError("invalid argument count")
        start = data.index(b"\0", 4) + 1
        while start < len(data) and data[start] == 0:
            start += 1
        values = data[start:].split(b"\0", argc)
        if len(values) <= argc:
            raise ValueError("incomplete arguments")
        return [os.fsdecode(value) for value in values[:argc]]
    data = _read(Path(f"/proc/{pid}/cmdline"), _LIMIT)
    return [os.fsdecode(value) for value in data.rstrip(b"\0").split(b"\0")] if data else []


def _read(path: Path, limit: int = _LIMIT) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        result = handle.read(limit + 1)
    if len(result) > limit:
        raise ValueError("file too large")
    return result


def _run_path(raw: Any, runs_root: Path) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or path.parent != runs_root or path.name.startswith("."):
        return None
    if path.is_symlink():
        return None
    return path


def _cli(argv: list[str], root: Path) -> tuple[str, list[str]] | None:
    if not argv:
        return None
    entry = Path(argv[0])
    if entry.parent != root / ".venv/bin":
        return None
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", entry.name):
        if len(argv) >= 3 and argv[1:3] in (["-m", "strixops.cli"], ["-m", "strixops.queue.worker"]):
            return ("worker" if argv[2].endswith(".worker") else "engine", argv[3:])
        if len(argv) >= 2 and argv[1] in (str(root / ".venv/bin/strix"), str(root / ".venv/bin/strixops")):
            return "engine", argv[2:]
    elif entry.name in {"strix", "strixops"}:
        return "engine", argv[1:]
    return None


def _argument(argv: list[str], name: str) -> str | None:
    values = [value[len(name) + 1 :] for value in argv if value.startswith(name + "=")]
    values.extend(argv[index + 1] for index, value in enumerate(argv[:-1]) if value == name)
    return values[0] if len(values) == 1 else None


def _capture(pid: int, identity: str, processes: Any, rows: dict, **fields: Any) -> dict | None:
    if type(pid) is not int or pid <= 1 or pid == os.getpid():
        return None
    if rows.get(pid, (None, None))[1] != os.getuid():
        return None
    actual = processes.process_identity(pid)
    if not actual or (identity and actual != identity):
        return None
    argv = [] if fields.get("kind") == "descendant" else _argv(pid)
    if processes.process_alive(pid, actual) is not True:
        return None
    return {
        "pid": pid,
        "identity": actual,
        "uid": os.getuid(),
        "parent_pid": rows[pid][0],
        "argv": argv,
        **fields,
    }


def snapshot_descendants(
    parent_pid: int,
    parent_identity: str,
    processes: Any,
) -> tuple[list[dict], list[str]]:
    """Capture same-user descendants of a caller-verified parent before it exits."""
    if not parent_identity or processes.process_alive(parent_pid, parent_identity) is not True:
        return [], []
    try:
        rows = _table()
        if rows.get(parent_pid, (None, None))[1] != os.getuid():
            return [], ["Process descendants: parent ownership could not be confirmed."]
        parents = {parent_pid: parent_identity}
        records = []
        issues = []
        for _ in range(len(rows)):
            added = False
            for pid, (parent, uid) in rows.items():
                if pid in parents or parent not in parents or uid != os.getuid():
                    continue
                if processes.process_alive(parent, parents[parent]) is not True:
                    continue
                try:
                    record = _capture(pid, "", processes, rows, kind="descendant", run_dir="")
                except (OSError, ValueError, subprocess.SubprocessError):
                    record = None
                if record and processes.process_alive(parent, parents[parent]) is True:
                    records.append(record)
                    parents[pid] = record["identity"]
                    added = True
                elif processes.process_identity(pid):
                    issues.append("Process descendants: a child identity could not be confirmed.")
            if not added:
                break
        return records, list(dict.fromkeys(issues))
    except (OSError, ValueError, subprocess.SubprocessError):
        return [], ["Process descendants: inventory could not be read."]


def _queue_rows(path: Path) -> list[dict]:
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink() or not path.is_file():
        raise ValueError("invalid queue database")
    with sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        result = [
            dict(row)
            for row in db.execute(
                "SELECT id,pid,start_identity,run_dir,owner_pid,owner_identity FROM items "
                "WHERE pid IS NOT NULL AND status NOT IN ('completed','failed','cancelled')"
            )
        ]
        result.extend(
            dict(row)
            for row in db.execute(
                "SELECT item_id AS id,pid,start_identity,run_dir FROM leases WHERE state != 'released'"
            )
        )
        return result


def discover_processes(root: Path, paths: dict, processes: Any) -> tuple[list[dict], list[str]]:
    """Find live owned engines and already-dead saved run identities read-only."""
    root = root.absolute()
    raw_root = paths.get("runs_root")
    if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
        return [], ["Scan processes: run directory configuration is invalid."]
    runs_root = Path(raw_root)
    if runs_root.is_symlink():
        return [], ["Scan processes: run directory is a symbolic link."]
    issues: list[str] = []
    try:
        rows = _table()
    except (OSError, ValueError, subprocess.SubprocessError):
        return [], ["Scan processes: process inventory could not be read."]
    saved: dict[int, list[dict]] = {}
    outside: set[tuple[int, str]] = set()
    try:
        queue_path = paths.get("queue_db")
        entries = _queue_rows(Path(queue_path)) if isinstance(queue_path, str) and queue_path else []
        for entry in entries:
            pid, identity = entry.get("pid"), entry.get("start_identity")
            raw_run = entry.get("run_dir")
            if isinstance(raw_run, str) and Path(raw_run).is_absolute() and Path(raw_run).parent != runs_root:
                if type(pid) is int and isinstance(identity, str):
                    outside.add((pid, identity))
                continue
            run_dir = _run_path(entry.get("run_dir"), runs_root)
            if (
                type(pid) is not int
                or pid <= 1
                or not isinstance(identity, str)
                or not identity
                or run_dir is None
            ):
                issues.append("Task queue processes: a saved process identity is invalid.")
                continue
            saved.setdefault(pid, []).append({**entry, "run_dir": str(run_dir)})
    except (OSError, ValueError, sqlite3.Error):
        issues.append("Task queue processes: saved state could not be read.")
    try:
        directories = list(runs_root.iterdir()) if runs_root.exists() else []
        for directory in directories:
            if directory.name.startswith(".") or not directory.is_dir() or directory.is_symlink():
                continue
            pid_path = directory / ".console.pid"
            if not pid_path.exists() and not pid_path.is_symlink():
                continue
            try:
                raw = _read(pid_path, 64).strip()
                if not raw.isdigit() or int(raw) <= 1:
                    raise ValueError("invalid PID")
                saved.setdefault(int(raw), []).append({"run_dir": str(directory), "id": None})
            except (OSError, ValueError):
                issues.append("Scan processes: a saved PID could not be read.")
    except OSError:
        issues.append("Scan processes: run directories could not be read.")

    records: dict[tuple[int, str, str], dict] = {}
    for pid in set(saved) | {pid for pid, (_, uid) in rows.items() if uid == os.getuid()}:
        candidates = saved.get(pid, [])
        try:
            # A mismatched birth identity proves only the saved worker is gone.
            # It never authorizes a signal to the new occupant of the PID.
            for entry in candidates:
                identity = entry.get("start_identity") or ""
                if pid not in rows or (identity and processes.process_alive(pid, identity) is False):
                    key = (pid, identity, entry["run_dir"])
                    records[key] = {
                        "pid": pid,
                        "identity": identity,
                        "kind": "saved",
                        "run_dir": entry["run_dir"],
                        "already_stopped": True,
                    }
            record = _capture(pid, "", processes, rows, kind="engine", run_dir="")
            if not record:
                if candidates and pid in rows:
                    # A recycled PID owned by somebody else is not our worker.
                    if rows[pid][1] == os.getuid() and processes.process_identity(pid):
                        continue
                    if rows[pid][1] == os.getuid():
                        issues.append("Scan processes: a live PID identity could not be confirmed.")
                continue
            match = _cli(record["argv"], root)
            if match is None:
                continue
            kind, arguments = match
            if kind == "worker":
                if (pid, record["identity"]) in outside:
                    continue
                item = _argument(arguments, "--item")
                matching = [
                    entry
                    for entry in candidates
                    if entry.get("id") == item and item and entry.get("start_identity") == record["identity"]
                ]
                if not matching:
                    issues.append("Task queue processes: worker ownership could not be confirmed.")
                    continue
                run_dir = Path(matching[0]["run_dir"])
                # The batch controller can launch replacements while workers
                # stop. Its persisted owner birth and live parent relationship
                # scope this supervisor to the verified queue worker.
                for entry in matching:
                    parent_pid, owner_identity = entry.get("owner_pid"), entry.get("owner_identity")
                    if parent_pid != record["parent_pid"] or not owner_identity:
                        continue
                    owner = _capture(
                        parent_pid, owner_identity, processes, rows, kind="scheduler", run_dir=""
                    )
                    owner_cli = _cli(owner["argv"], root) if owner else None
                    if owner_cli and owner_cli[0] == "engine":
                        records[(parent_pid, owner["identity"], "")] = owner
            else:
                instruction = _argument(arguments, "--instruction-file")
                run_dir = _run_path(str(Path(instruction).parent), runs_root) if instruction else None
                if run_dir is None:
                    continue
                # A queue record with a different identity must not confer any
                # ownership; direct engine ownership comes from its exact argv.
            record.update(kind=kind, run_dir=str(run_dir))
            records[(pid, record["identity"], str(run_dir))] = record
        except (OSError, ValueError, subprocess.SubprocessError):
            if candidates:
                issues.append("Scan processes: saved PID ownership could not be confirmed.")
    return list(records.values()), list(dict.fromkeys(issues))


def _alive(record: dict, processes: Any) -> bool | None:
    if record.get("already_stopped"):
        return False
    return processes.process_alive(record["pid"], record["identity"])


def _signal(record: dict, value: int, processes: Any) -> bool:
    pid = record["pid"]
    if type(pid) is not int or pid <= 1 or pid == os.getpid() or not record.get("identity"):
        return False
    if _alive(record, processes) is not True:
        return False
    rows = _table()
    if rows.get(pid, (None, None))[1] != os.getuid():
        return False
    if record.get("kind") != "descendant" and _argv(pid) != record.get("argv"):
        return False
    if _alive(record, processes) is not True:
        return False
    os.kill(pid, value)
    return True


def stop_processes(
    records: list[dict],
    *,
    processes: Any,
    timeout: float = 5.0,
) -> tuple[list[dict], list[str]]:
    """TERM verified owners and descendants, then KILL survivors within a bound."""
    owned = {(record["pid"], record.get("identity", "")): dict(record) for record in records}
    # Console descendants may already be orphaned by the time discovery runs.
    # Preserve the captured parent relationships for conservative run-state
    # reconciliation. These path associations never authorize a signal.
    run_paths = {record["pid"]: record["run_dir"] for record in records if record.get("run_dir")}
    for _ in range(len(owned)):
        added = False
        for record in owned.values():
            path = run_paths.get(record["pid"]) or run_paths.get(record.get("parent_pid"))
            if path and not record.get("run_dir"):
                record["run_dir"] = path
                run_paths[record["pid"]] = path
                added = True
        if not added:
            break
    issues: list[str] = []
    uncertain_runs: set[str] = set()
    sent: set[tuple[int, str]] = set()
    deadline = time.monotonic() + max(0.0, timeout)

    def expand() -> None:
        for record in list(owned.values()):
            if _alive(record, processes) is not True:
                continue
            descendants, errors = snapshot_descendants(record["pid"], record["identity"], processes)
            issues.extend(errors)
            if errors and record.get("run_dir"):
                uncertain_runs.add(record["run_dir"])
            for child in descendants:
                child["run_dir"] = record.get("run_dir", "")
                existing = owned.setdefault((child["pid"], child["identity"]), child)
                if not existing.get("run_dir") and child["run_dir"]:
                    existing["run_dir"] = child["run_dir"]

    while True:
        expand()
        for key, record in sorted(owned.items(), key=lambda item: item[1].get("kind") != "scheduler"):
            if key in sent or _alive(record, processes) is not True:
                continue
            try:
                if _signal(record, signal.SIGTERM, processes):
                    sent.add(key)
            except ProcessLookupError:
                pass
            except (OSError, ValueError, subprocess.SubprocessError):
                issues.append("Scan processes: a process could not be signalled.")
        live = [record for record in owned.values() if _alive(record, processes) is True]
        if not live or time.monotonic() >= deadline:
            break
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    expand()
    for record in owned.values():
        if _alive(record, processes) is not True:
            continue
        try:
            _signal(record, signal.SIGKILL, processes)
        except ProcessLookupError:
            pass
        except (OSError, ValueError, subprocess.SubprocessError):
            issues.append("Scan processes: a process could not be force-stopped.")
    kill_deadline = time.monotonic() + 2.0
    while (
        any(_alive(record, processes) is True for record in owned.values())
        and time.monotonic() < kill_deadline
    ):
        time.sleep(0.05)
    remaining = [record for record in owned.values() if _alive(record, processes) is not False]
    uncertain_runs.update(record["run_dir"] for record in remaining if record.get("run_dir"))
    stopped = [
        record
        for record in owned.values()
        if _alive(record, processes) is False and record.get("run_dir", "") not in uncertain_runs
    ]
    if remaining:
        issues.append("Scan processes: some process identities remain active or unconfirmed.")
    return stopped, list(dict.fromkeys(issues))
