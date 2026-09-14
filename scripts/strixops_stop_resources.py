"""Finish an explicit stop after the managed Console and scan processes exit.

This module does not import application stores (which can start recovery), delete
containers, or touch evidence. A runtime Python with Docker installed may execute
it as a JSON subprocess; importing it needs only the standard library.
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_RUN_OWNER = "io.strixops.run-owner"
_MCP_OWNER = "io.strixops.mcp.owner"
_MCP_TASK = "io.strixops.mcp.task"
_MCP_SESSION = "io.strixops.mcp.session"
_MCP_ROLE = "io.strixops.mcp.role"
_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
_TASK_ACTIVE = {"starting", "capturing", "stopping", "ending", "deleting", "delete_failed"}
_JOB_ACTIVE = {"queued", "starting", "running", "cancelling"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")
_OWNER = re.compile(r"[0-9a-f]{32}\Z")


class StopResourceError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017 -- fallback Python 3.9


def _safe(path: Path, *, directory: bool = False) -> bool:
    """Reject links, including ancestors, without turning absence into an error."""
    for ancestor in reversed(path.absolute().parents):
        try:
            mode = ancestor.lstat().st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(mode):
            raise StopResourceError("unsafe path")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise StopResourceError("unsafe path")
    return True


def _json(path: Path) -> dict:
    if not _safe(path):
        return {}
    with path.open("rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise StopResourceError("oversized state")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise StopResourceError("invalid state")
    return value


def _write_json(path: Path, value: dict) -> None:
    if not _safe(path):
        raise StopResourceError("missing state")
    descriptor, name = tempfile.mkstemp(prefix=".stopped-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


@contextlib.contextmanager
def _database(path: Path):
    if not _safe(path):
        yield None
        return
    for suffix in ("-wal", "-shm", "-journal"):
        _safe(Path(str(path) + suffix))
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


@contextlib.contextmanager
def _lock(path: Path):
    _safe(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _process_alive(pid, identity=""):
    spec = importlib.util.spec_from_file_location(
        "strixops_stop_process_identity", _ROOT / "src/strixops/queue/processes.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.process_alive(pid, identity)


class _Docker:
    def __init__(self, timeout: float):
        self.deadline = time.monotonic() + max(0, timeout)
        self.client = None
        self.module = None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise StopResourceError("container shutdown deadline exceeded")
        return remaining

    def connect(self):
        if self.client is None:
            import docker

            self.module = docker
            self.client = docker.from_env(timeout=max(1, min(10, self.remaining())))
        self.remaining()
        if getattr(self.client, "api", None) is not None:
            self.client.api.timeout = max(1, min(10, self.remaining()))
        return self.client

    def inspect(self, container_id: str, labels: dict, daemon: str = ""):
        if not isinstance(container_id, str) or not _CONTAINER.fullmatch(container_id):
            raise StopResourceError("missing container identity")
        client = self.connect()
        if daemon and client.info().get("ID") != daemon:
            raise StopResourceError("Docker daemon identity changed")
        try:
            container = client.containers.get(container_id)
            container.reload()
        except self.module.errors.NotFound:
            return None
        attrs = container.attrs
        actual = attrs.get("Config", {}).get("Labels") or {}
        if container.id != container_id or attrs.get("Id") != container_id or any(
            actual.get(key) != value for key, value in labels.items()
        ):
            raise StopResourceError("container ownership changed")
        return container

    def stop(self, container_id: str, labels: dict, daemon: str = "") -> str:
        container = self.inspect(container_id, labels, daemon)
        if container is None:
            return "absent"
        state = container.attrs.get("State", {})
        if state.get("Paused") is True:
            container.unpause()
            container = self.inspect(container_id, labels, daemon)
            if container is None:
                return "absent"
            state = container.attrs.get("State", {})
        if state.get("Running") is not False or state.get("Restarting") is True:
            # Docker stop itself escalates to SIGKILL after this grace period.
            try:
                container.stop(timeout=max(0, min(5, int(self.remaining()) - 1)))
            except Exception:
                container = self.inspect(container_id, labels, daemon)
                if container is not None:
                    container.kill()
            container = self.inspect(container_id, labels, daemon)
        if container is None:
            return "absent"
        state = container.attrs.get("State", {})
        if (
            state.get("Running") is not False or state.get("Restarting") is True
            or state.get("Paused") is True
        ):
            raise StopResourceError("container still running")
        return "stopped"

    def stop_owner(self, owner: str, daemon: str) -> str:
        client = self.connect()
        if client.info().get("ID") != daemon:
            raise StopResourceError("Docker daemon identity changed")
        containers = client.containers.list(all=True, filters={"label": f"{_RUN_OWNER}={owner}"})
        result = "absent"
        for container in containers:
            if self.stop(container.id, {_RUN_OWNER: owner}, daemon) == "stopped":
                result = "stopped"
        return result

    def close(self):
        if self.client is not None:
            with contextlib.suppress(Exception):
                self.client.close()


def _owned_run(value, root: Path) -> bool:
    return isinstance(value, str) and bool(value) and Path(value).absolute().parent == root.absolute()


def _owned_batch(batch: dict, path: Path, root: Path) -> bool:
    if batch.get("owner") == "console:" + str(root.absolute()):
        return True
    reference = batch.get("snapshot_ref", "")
    if not isinstance(reference, str) or not _OWNER.fullmatch(reference):
        return False
    if batch.get("owner") != "cli:v1:" + reference:
        return False
    try:
        snapshot = _json(path.parent / "scan_queue_snapshots" / f"{reference}.json")
        settings = snapshot.get("settings") or {}
        return snapshot.get("executor") == "cli:v1" and settings.get("strix_runs") == str(root.absolute())
    except Exception:
        return False


def _queue(path: Path, root: Path, docker: _Docker, issues: list[str], stopped_runs: set[str]) -> None:
    if not _safe(path):
        return
    with _lock(Path(str(path) + ".scheduler.lock")), _database(path) as db:
        leases = [dict(row) for row in db.execute("SELECT * FROM leases")]
        items = [dict(row) for row in db.execute("SELECT * FROM items")]
        batches = [dict(row) for row in db.execute("SELECT * FROM batches")]
        owned_batches = {batch["id"] for batch in batches if _owned_batch(batch, path, root)}
        lease_results = {}
        for lease in leases:
            if lease["state"] == "released" or not _owned_run(lease.get("run_dir"), root):
                continue
            if _process_alive(lease.get("pid"), lease.get("start_identity") or "") is not False:
                issues.append("Task queue: a lease process could not be confirmed stopped.")
                continue
            try:
                result = "absent"
                if not lease.get("cleanup_complete") and lease.get("resource_phase") != "not_started":
                    owner = lease.get("resource_owner", "")
                    daemon = lease.get("resource_daemon", "")
                    if not isinstance(owner, str) or not _OWNER.fullmatch(owner) or not daemon:
                        raise StopResourceError("missing sandbox identity")
                    container_id = lease.get("resource_container", "")
                    result = (
                        docker.stop(container_id, {_RUN_OWNER: owner}, daemon)
                        if container_id else docker.stop_owner(owner, daemon)
                    )
                db.execute(
                    "UPDATE leases SET state='released',cleanup_complete=?,updated_at=? WHERE token=?",
                    (int(result == "absent"), time.time(), lease["token"]),
                )
                lease_results[lease.get("item_id")] = result
                if isinstance(lease.get("run_dir"), str):
                    stopped_runs.add(lease["run_dir"])
            except Exception:
                issues.append("Task queue: a saved sandbox could not be verified or stopped.")
        for item in items:
            if item["status"] in _TERMINAL or not (
                item.get("batch_id") in owned_batches or _owned_run(item.get("run_dir"), root)
            ):
                continue
            alive = _process_alive(item.get("pid"), item.get("start_identity") or "")
            unstarted = item["status"] == "queued" and not item.get("pid")
            owner_dead = not item.get("owner_pid") or _process_alive(
                item.get("owner_pid"), item.get("owner_identity") or ""
            ) is False
            item_leases = [lease for lease in leases if lease.get("item_id") == item["id"]]
            clean = not item_leases or item["id"] in lease_results or all(
                lease["state"] == "released" for lease in item_leases
            )
            if (unstarted or alive is False) and owner_dead and clean:
                db.execute(
                    "UPDATE items SET status='cancelled',cancel_requested=1,stop_sent=1,"
                    "error_code='queue_cancelled',updated_at=? WHERE id=?",
                    (time.time(), item["id"]),
                )
                if isinstance(item.get("run_dir"), str):
                    stopped_runs.add(item["run_dir"])
            else:
                db.execute(
                    "UPDATE items SET cancel_requested=1,updated_at=? WHERE id=?",
                    (time.time(), item["id"]),
                )
                issues.append("Task queue: an item remains unconfirmed; cancellation was recorded.")
        for batch_id in owned_batches:
            db.execute(
                "UPDATE batches SET cancel_requested=1,updated_at=? WHERE id=?", (time.time(), batch_id)
            )


def _records(db, table: str) -> list[dict]:
    known = {
        "tasks": _TASK_ACTIVE | {"idle", "stopped", "ended", "error"},
        "sessions": {"starting", "running", "stopping", "stopped", "error"},
        "jobs": _JOB_ACTIVE | {"completed", "failed", "cancelled", "blocked"},
    }
    result = []
    for row in db.execute(f"SELECT * FROM {table}"):
        value = json.loads(row["data"])
        if not isinstance(value, dict) or value.get("id") != row["id"] or not _ID.fullmatch(row["id"]):
            raise StopResourceError("invalid MCP record")
        if table != "tasks" and value.get("task_id") != row["task_id"]:
            raise StopResourceError("invalid MCP relationship")
        if value.get("status") not in known[table]:
            raise StopResourceError("invalid MCP status")
        result.append(value)
    return result


def _capture_metadata(root: Path, session: dict) -> dict:
    identity, task = session["id"], session["task_id"]
    metadata = dict(session)
    if not session.get("container_id"):
        directory = root / "tasks" / task / "captures" / identity
        if _safe(directory, directory=True):
            receipt = _json(directory / "runtime.json")
            if receipt:
                if receipt.get("task_id") != task or receipt.get("session_id") != identity:
                    raise StopResourceError("capture receipt mismatch")
                metadata.update(receipt)
    if metadata.get("session_id", identity) != identity:
        raise StopResourceError("capture session mismatch")
    return metadata


def _task_containers(task: str, root: Path, docker: _Docker) -> None:
    """Cover create-before-receipt gaps using task labels and exact bind mounts."""
    client = docker.connect()
    containers = client.containers.list(
        all=True, filters={"label": f"{_MCP_TASK}={task}"}
    )
    failures = False
    for container in containers:
        try:
            _stop_discovered(container, task, root, docker)
        except Exception:
            failures = True
    if failures:
        raise StopResourceError("some MCP containers could not be stopped")


def _stop_discovered(container, task: str, root: Path, docker: _Docker) -> None:
    docker.remaining()
    try:
        container.reload()
    except docker.module.errors.NotFound:
        return
    else:
        attrs = container.attrs
        labels = attrs.get("Config", {}).get("Labels") or {}
        owner, session, role = (
            labels.get(_MCP_OWNER, ""), labels.get(_MCP_SESSION, ""), labels.get(_MCP_ROLE, "")
        )
        if not isinstance(owner, str) or not _OWNER.fullmatch(owner) or not _ID.fullmatch(session):
            raise StopResourceError("invalid MCP container identity")
        if role == "capture":
            expected_source = str(root / "tasks" / task / "captures" / session)
            destination, writable = "/capture", True
        elif role == "replay":
            expected_source = str(_ROOT / "src/strixops/traffic")
            destination, writable = "/opt/traffic", False
        else:
            raise StopResourceError("unknown MCP container role")
        if not any(
            mount.get("Type") == "bind" and mount.get("Source") == expected_source
            and mount.get("Destination") == destination and mount.get("RW") is writable
            for mount in attrs.get("Mounts", [])
        ):
            raise StopResourceError("MCP project ownership mismatch")
        docker.stop(container.id, {
            _MCP_OWNER: owner, _MCP_TASK: task, _MCP_SESSION: session, _MCP_ROLE: role,
        })


def _mcp(root: Path, docker: _Docker, issues: list[str]) -> None:
    if not _safe(root, directory=True):
        return
    with _database(root / "traffic.sqlite3") as db:
        if db is None:
            return
        tasks, sessions, jobs = (_records(db, table) for table in ("tasks", "sessions", "jobs"))
        task_ids = {task["id"] for task in tasks}
        if any(record["task_id"] not in task_ids for record in [*sessions, *jobs]):
            raise StopResourceError("orphan MCP relationship")
        for task in tasks:
            related = [session for session in sessions if session["task_id"] == task["id"]]
            active_jobs = [
                job for job in jobs
                if job["task_id"] == task["id"] and job.get("status") in _JOB_ACTIVE
            ]
            success = True
            inventory_verified = False
            if active_jobs or task.get("status") in _TASK_ACTIVE or any(
                session.get("status") != "stopped" for session in related
            ):
                try:
                    _task_containers(task["id"], root, docker)
                    inventory_verified = True
                except Exception:
                    success = False
                    issues.append("MCP tasks: related containers could not all be verified or stopped.")
            for session in related:
                if session.get("status") == "stopped":
                    continue
                try:
                    metadata = _capture_metadata(root, session)
                    owner = metadata.get("owner_token", "")
                    if metadata.get("container_id") or owner:
                        if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
                            raise StopResourceError("missing capture owner")
                        docker.stop(metadata.get("container_id", ""), {
                            _MCP_OWNER: owner, _MCP_TASK: task["id"],
                            _MCP_SESSION: session["id"], _MCP_ROLE: "capture",
                        })
                    elif not inventory_verified:
                        raise StopResourceError("missing capture owner")
                    # Leave offsets, journal and evidence untouched for normal ingestion.
                    session.update(status="stopped", stopped_at=_now(), stop_reason="operator_stopped")
                    db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), session["id"]))
                except Exception:
                    success = False
                    issues.append("MCP capture: a saved container could not be verified or stopped.")
            if active_jobs:
                try:
                    if not inventory_verified:
                        raise StopResourceError("request containers unconfirmed")
                    for job in active_jobs:
                        job.update(
                            status="cancelled", cancel_requested=True,
                            finished_at=_now(), error="Operator stopped services",
                        )
                        db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job["id"]))
                except Exception:
                    success = False
                    issues.append("MCP request tests: replay containers could not be verified or stopped.")
            if success and task.get("status") in _TASK_ACTIVE:
                task.update(status="stopped", updated_at=_now(), stop_reason="operator_stopped")
                db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task), task["id"]))


def _fofa(root: Path) -> None:
    if not _safe(root, directory=True):
        return
    with _lock(root / ".search.lock"), _database(root / "history.sqlite3") as db:
        if db is not None:
            db.execute(
                "UPDATE searches SET status=CASE WHEN loaded_count>0 THEN 'partial' ELSE 'failed' END,"
                "error_code='interrupted',updated_at=? WHERE status IN ('queued','running')",
                (_now(),),
            )


def _runs(root: Path, stopped_runs: set[str], issues: list[str]) -> None:
    if not _safe(root, directory=True):
        return
    for name in stopped_runs:
        try:
            directory = Path(name).absolute()
            if directory.parent != root.absolute() or not _safe(directory, directory=True):
                raise StopResourceError("run path outside configured root")
            path = directory / "run.json"
            record = _json(path)
            if not record:
                continue
            cleanup = record.get("cleanup")
            if record.get("status") == "running" or (
                isinstance(cleanup, dict) and cleanup.get("status") == "in_progress"
            ):
                if record.get("status") == "running":
                    record["status"] = "interrupted"
                record["operator_stop"] = {"at": _now(), "process_stopped": True}
                if isinstance(cleanup, dict) and cleanup.get("status") == "in_progress":
                    cleanup["status"] = "interrupted"
                _write_json(path, record)
        except Exception:
            issues.append("Scan runs: a stopped run record could not be reconciled; its data was retained.")


def cleanup_resources(
    paths: dict, *, timeout: float = 30, stopped_runs=None, reconcile_runs: bool = True
) -> list[str]:
    """Return bounded diagnostics; caller must first stop managed host processes."""
    issues = []
    stopped = set(stopped_runs or [])
    docker = _Docker(timeout)
    try:
        for key, label, callback in (
            ("queue_db", "Task queue", lambda path: _queue(
                path, Path(paths["runs_root"]).expanduser(), docker, issues, stopped
            )),
            ("mcp_root", "MCP tasks", lambda path: _mcp(path, docker, issues)),
            ("fofa_root", "FOFA searches", _fofa),
            ("runs_root", "Scan runs", lambda path: _runs(path, stopped, issues) if reconcile_runs else None),
        ):
            try:
                value = paths.get(key)
                if not isinstance(value, str) or not value:
                    raise StopResourceError("missing configured path")
                callback(Path(value).expanduser())
            except Exception:
                issues.append(
                    f"{label}: stored state was unreadable or could not be reconciled; it was retained."
                )
    finally:
        docker.close()
    return list(dict.fromkeys(issues))


def main() -> int:
    try:
        request = json.loads(sys.stdin.read(1024 * 1024))
        issues = cleanup_resources(
            request["paths"], timeout=float(request.get("timeout", 30)),
            stopped_runs=request.get("stopped_runs", []),
            reconcile_runs=request.get("reconcile_runs", True) is True,
        )
    except Exception:
        issues = ["Resource cleanup request was invalid; saved state was retained."]
    print(json.dumps({"issues": issues}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
