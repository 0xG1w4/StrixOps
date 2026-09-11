"""Persistent per-target scheduling and host capacity, without launch credentials."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from strixops.engine.targets import normalize_targets
from strixops.platform.runname import derive_target_label, slugify_for_run_name

from .processes import process_alive, process_identity

TERMINAL = frozenset({"completed", "failed", "cancelled"})
ACTIVE = frozenset({"starting", "waiting_capacity", "running", "cancelling"})
ID_RE = re.compile(r"^[a-f0-9]{32}$")
ERRORS = {
    "scope_changed": "The target no longer matches its project scope.",
    "invalid_snapshot": "The saved launch configuration is invalid.",
    "snapshot_unavailable": "The saved launch configuration is unavailable.",
    "different_runs_root": "This batch belongs to another task directory.",
    "invalid_run": "The queued task identity is invalid.",
    "queue_not_found": "The batch or target was not found.",
    "queue_invalid": "The queue request is invalid.",
    "queue_busy": "The batch still has active, queued, or blocked targets.",
    "queue_cancelled": "This target was cancelled before it could start.",
    "queue_launch_failed": "The target could not be started.",
    "queue_launch_uncertain": "Startup could not be confirmed; resource cleanup must be verified.",
    "queue_cleanup_unconfirmed": "Resource cleanup could not be confirmed.",
    "queue_process_unconfirmed": "The original scan process could not be verified.",
    "queue_scan_failed": "The target assessment did not complete successfully.",
    "queue_scope_rejected": "The target is no longer within the project's authorized scope.",
    "queue_snapshot_invalid": "The saved launch configuration is unavailable or invalid.",
    "queue_stop_failed": "Stopping the target could not be confirmed.",
}


class QueueError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code if code in ERRORS else "queue_invalid"
        # Callers can provide a deliberately safe validation message.
        self.message = message or ERRORS[self.code]
        super().__init__(self.message)


def _id(value: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise QueueError("queue_invalid")
    return value


def _limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 16:
        raise QueueError("queue_invalid")
    return value


def default_queue_path() -> Path:
    override = os.environ.get("STRIXOPS_QUEUE_DB", "").strip()
    if override:
        return Path(override).expanduser().absolute()
    from strixops.console.settings_store import config_path

    return config_path().parent / "scan_queue.sqlite3"


class QueueStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (Path(path) if path is not None else default_queue_path()).expanduser().absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise QueueError("queue_invalid")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise QueueError("queue_invalid")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self._db() as db:
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS queue_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), max_active_targets INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO queue_settings VALUES(1, 2);
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, max_concurrent INTEGER NOT NULL,
                    scan_type TEXT NOT NULL, snapshot_ref TEXT NOT NULL,
                    project_id TEXT NOT NULL, source_json TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, last_started REAL NOT NULL DEFAULT 0,
                    owner TEXT NOT NULL DEFAULT 'default'
                );
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                    ordinal INTEGER NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
                    run_name TEXT UNIQUE, run_dir TEXT, pid INTEGER, start_identity TEXT,
                    launch_token TEXT, owner_pid INTEGER, owner_identity TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    stop_sent INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT, error TEXT, report_ready INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS items_batch ON items(batch_id, ordinal);
                CREATE INDEX IF NOT EXISTS items_status ON items(status);
                CREATE TABLE IF NOT EXISTS leases (
                    token TEXT PRIMARY KEY, run_key TEXT UNIQUE NOT NULL, item_id TEXT UNIQUE,
                    run_name TEXT NOT NULL, run_dir TEXT NOT NULL,
                    pid INTEGER NOT NULL, start_identity TEXT NOT NULL,
                    state TEXT NOT NULL, cleanup_complete INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    resource_phase TEXT NOT NULL DEFAULT 'unknown', resource_owner TEXT NOT NULL DEFAULT '',
                    resource_container TEXT NOT NULL DEFAULT '', resource_daemon TEXT NOT NULL DEFAULT ''
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(batches)")}
            if "owner" not in columns:
                db.execute("ALTER TABLE batches ADD COLUMN owner TEXT NOT NULL DEFAULT 'default'")
            lease_columns = {row[1] for row in db.execute("PRAGMA table_info(leases)")}
            for column, default in (
                ("resource_phase", "unknown"),
                ("resource_owner", ""),
                ("resource_container", ""),
                ("resource_daemon", ""),
            ):
                if column not in lease_columns:
                    db.execute(f"ALTER TABLE leases ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def create_batch(
        self,
        *,
        targets: list[str],
        scan_type: str,
        snapshot_ref: str,
        name: str = "",
        max_concurrent: int = 2,
        project_id: str = "",
        source: dict | None = None,
        owner: str = "default",
    ) -> dict:
        try:
            targets = normalize_targets(targets=targets)
            for target in targets:
                target.encode("utf-8", "strict")
            for value in (name, project_id, owner):
                if not isinstance(value, str):
                    raise ValueError
                value.encode("utf-8", "strict")
            if source is not None and not isinstance(source, dict):
                raise ValueError
            source_json = json.dumps(source or {}, ensure_ascii=True, allow_nan=False)
        except (ValueError, TypeError, UnicodeError):
            raise QueueError("queue_invalid") from None
        _id(snapshot_ref)
        _limit(max_concurrent)
        if (
            scan_type not in ("web", "internal")
            or not isinstance(name, str)
            or len(name) > 200
            or len(source_json) > 8192
            or not isinstance(project_id, str)
            or len(project_id) > 200
            or not isinstance(owner, str)
            or not 1 <= len(owner) <= 4096
        ):
            raise QueueError("queue_invalid")
        batch_id, now = uuid.uuid4().hex, time.time()
        with self._db() as db:
            db.execute(
                "INSERT INTO batches(id,name,created_at,updated_at,max_concurrent,scan_type,"
                "snapshot_ref,project_id,source_json,owner) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    name.strip(),
                    now,
                    now,
                    max_concurrent,
                    scan_type,
                    snapshot_ref,
                    project_id,
                    source_json,
                    owner,
                ),
            )
            for ordinal, target in enumerate(targets):
                db.execute(
                    "INSERT INTO items(id,batch_id,ordinal,target,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, batch_id, ordinal, target, "queued", now, now),
                )
        return self.get_batch(batch_id)

    @staticmethod
    def _public(db: sqlite3.Connection, batch: sqlite3.Row, *, detail: bool = True) -> dict:
        rows = db.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch["id"],)).fetchall()
        states = Counter(row["status"] for row in rows)
        counts = {
            "queued": states["queued"],
            "active": sum(states[s] for s in ACTIVE),
            "completed": states["completed"],
            "failed": states["failed"],
            "cancelled": states["cancelled"],
            "blocked": states["blocked"],
        }
        if counts["active"]:
            status = "cancelling" if batch["cancel_requested"] else "running"
        elif counts["blocked"]:
            status = "blocked"
        elif counts["queued"]:
            status = "queued"
        elif counts["cancelled"] == len(rows):
            status = "cancelled"
        elif counts["failed"] == len(rows):
            status = "failed"
        else:
            status = "completed"
        result: dict[str, Any] = {
            "id": batch["id"],
            "name": batch["name"],
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
            "status": status,
            "max_concurrent": batch["max_concurrent"],
            "target_count": len(rows),
            "counts": counts,
        }
        if detail:
            result["items"] = [
                {
                    "id": row["id"],
                    "target": row["target"],
                    "scan_type": batch["scan_type"],
                    "status": row["status"],
                    "run_name": row["run_name"],
                    "error_code": row["error_code"],
                    "error": row["error"],
                    "report_ready": bool(row["report_ready"]),
                }
                for row in rows
            ]
        source = json.loads(batch["source_json"])
        if source:
            result["source"] = source
        if batch["project_id"]:
            result["project_id"] = batch["project_id"]
        return result

    def get_batch(self, batch_id: str, *, owner: str | None = None) -> dict:
        _id(batch_id)
        with self._db() as db:
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None or (owner is not None and batch["owner"] != owner):
                raise QueueError("queue_not_found")
            return self._public(db, batch)

    def list_batches(self, limit: int = 50, offset: int = 0, *, owner: str | None = None) -> dict:
        if (
            not isinstance(limit, int)
            or not isinstance(offset, int)
            or isinstance(limit, bool)
            or isinstance(offset, bool)
            or not 1 <= limit <= 200
            or offset < 0
        ):
            raise QueueError("queue_invalid")
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM batches"
                + (" WHERE owner=?" if owner is not None else "")
                + " ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                (owner, limit, offset) if owner is not None else (limit, offset),
            ).fetchall()
            return {
                "batches": [self._public(db, row, detail=False) for row in rows],
                "total": db.execute(
                    "SELECT COUNT(*) FROM batches" + (" WHERE owner=?" if owner is not None else ""),
                    (owner,) if owner is not None else (),
                ).fetchone()[0],
            }

    @staticmethod
    def _reap(db: sqlite3.Connection) -> None:
        for row in db.execute(
            "SELECT * FROM leases WHERE state IN ('active','draining','waiting')"
        ).fetchall():
            alive = process_alive(row["pid"], row["start_identity"])
            if alive is not False:
                continue
            state = "released" if row["cleanup_complete"] or row["state"] == "waiting" else "blocked"
            db.execute(
                "UPDATE leases SET state=?,updated_at=? WHERE token=?", (state, time.time(), row["token"])
            )

    @staticmethod
    def _occupied(db: sqlite3.Connection) -> int:
        leases = db.execute(
            "SELECT COUNT(*) FROM leases WHERE state IN ('active','draining','blocked')"
        ).fetchone()[0]
        reservations = db.execute(
            "SELECT COUNT(*) FROM items i WHERE status IN ('starting','waiting_capacity','running',"
            "'cancelling','blocked') AND NOT EXISTS(SELECT 1 FROM leases l WHERE l.item_id=i.id)",
        ).fetchone()[0]
        return leases + reservations

    def settings(self) -> dict:
        with self._db() as db:
            self._reap(db)
            return self._settings(db)

    @classmethod
    def _settings(cls, db: sqlite3.Connection) -> dict:
        blocked = db.execute("SELECT COUNT(*) FROM leases WHERE state='blocked'").fetchone()[0]
        blocked += db.execute(
            "SELECT COUNT(*) FROM items i WHERE status='blocked' "
            "AND NOT EXISTS(SELECT 1 FROM leases l WHERE l.item_id=i.id "
            "AND (l.state='blocked' OR l.cleanup_complete=1))",
        ).fetchone()[0]
        waiting = db.execute("SELECT COUNT(*) FROM items WHERE status='queued'").fetchone()[0]
        waiting += db.execute("SELECT COUNT(*) FROM leases WHERE state='waiting'").fetchone()[0]
        return {
            "max_active_targets": db.execute("SELECT max_active_targets FROM queue_settings").fetchone()[0],
            "active_targets": max(0, cls._occupied(db) - blocked),
            "waiting_targets": waiting,
            "blocked_targets": blocked,
        }

    def update_settings(self, max_active_targets: int) -> dict:
        _limit(max_active_targets)
        with self._db() as db:
            db.execute("UPDATE queue_settings SET max_active_targets=? WHERE id=1", (max_active_targets,))
            self._reap(db)
            return self._settings(db)

    def cancel_batch(self, batch_id: str, *, owner: str | None = None) -> dict:
        self.get_batch(batch_id, owner=owner)
        return self._cancel(batch_id, None)

    def cancel_item(self, batch_id: str, item_id: str, *, owner: str | None = None) -> dict:
        self.get_batch(batch_id, owner=owner)
        return self._cancel(batch_id, _id(item_id))

    def _cancel(self, batch_id: str, item_id: str | None) -> dict:
        _id(batch_id)
        with self._db() as db:
            if db.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone() is None:
                raise QueueError("queue_not_found")
            rows = db.execute(
                "SELECT * FROM items WHERE batch_id=?" + (" AND id=?" if item_id else ""),
                (batch_id, item_id) if item_id else (batch_id,),
            ).fetchall()
            if item_id and not rows:
                raise QueueError("queue_not_found")
            now = time.time()
            for row in rows:
                if row["status"] in TERMINAL:
                    continue
                status = (
                    "cancelled"
                    if row["status"] == "queued"
                    else ("blocked" if row["status"] == "blocked" else "cancelling")
                )
                db.execute(
                    "UPDATE items SET status=?,cancel_requested=1,updated_at=? WHERE id=?",
                    (status, now, row["id"]),
                )
            db.execute(
                "UPDATE batches SET updated_at=?,cancel_requested=MAX(cancel_requested,?) WHERE id=?",
                (now, int(item_id is None), batch_id),
            )
        return self.get_batch(batch_id)

    def delete_batch(self, batch_id: str, *, owner: str | None = None) -> str:
        _id(batch_id)
        with self._db() as db:
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None or (owner is not None and batch["owner"] != owner):
                raise QueueError("queue_not_found")
            if db.execute(
                "SELECT 1 FROM items WHERE batch_id=? AND status NOT IN ('completed','failed','cancelled')",
                (batch_id,),
            ).fetchone():
                raise QueueError("queue_busy")
            db.execute(
                "DELETE FROM leases WHERE item_id IN (SELECT id FROM items WHERE batch_id=?)", (batch_id,)
            )
            db.execute("DELETE FROM items WHERE batch_id=?", (batch_id,))
            db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            return str(batch["snapshot_ref"])

    @staticmethod
    def _item_query() -> str:
        return (
            "SELECT i.*,b.scan_type,b.snapshot_ref,b.project_id,b.owner FROM items i "
            "JOIN batches b ON b.id=i.batch_id "
        )

    def internal_item(self, item_id: str) -> dict:
        with self._db() as db:
            row = db.execute(self._item_query() + "WHERE i.id=?", (_id(item_id),)).fetchone()
            if row is None:
                raise QueueError("queue_not_found")
            return dict(row)

    def pending_items(self, *, owner: str = "default") -> list[dict]:
        with self._db() as db:
            return [
                dict(row)
                for row in db.execute(
                    self._item_query() + "WHERE i.status NOT IN ('queued','completed','failed','cancelled') "
                    "AND b.owner=? ORDER BY i.created_at,i.ordinal",
                    (owner,),
                )
            ]

    def claim_next(self, *, owner: str = "default") -> dict | None:
        with self._db() as db:
            self._reap(db)
            maximum = db.execute("SELECT max_active_targets FROM queue_settings").fetchone()[0]
            if self._occupied(db) >= maximum:
                return None
            # Existing direct-run waiters entered before a new scheduler claim.
            if db.execute("SELECT 1 FROM leases WHERE state='waiting'").fetchone():
                return None
            row = db.execute(
                self._item_query() + "WHERE i.status='queued' AND b.cancel_requested=0 AND b.owner=? "
                "AND (SELECT COUNT(*) FROM items sibling WHERE sibling.batch_id=b.id AND "
                "sibling.status IN ('starting','waiting_capacity','running','cancelling','blocked')) "
                "< b.max_concurrent ORDER BY b.last_started,b.created_at,i.ordinal LIMIT 1",
                (owner,),
            ).fetchone()
            if row is None:
                return None
            prefix = slugify_for_run_name(derive_target_label(row["target"], row["scan_type"]))
            for _ in range(100):
                name = prefix + "_" + uuid.uuid4().hex[:4]
                if not db.execute("SELECT 1 FROM items WHERE run_name=?", (name,)).fetchone():
                    break
            else:
                raise QueueError("queue_launch_failed")
            now = time.time()
            db.execute(
                "UPDATE items SET status='starting',run_name=?,launch_token=?,owner_pid=?,"
                "owner_identity=?,updated_at=? WHERE id=?",
                (name, uuid.uuid4().hex, os.getpid(), process_identity(os.getpid()), now, row["id"]),
            )
            db.execute(
                "UPDATE batches SET last_started=?,updated_at=? WHERE id=?", (now, now, row["batch_id"])
            )
            return dict(db.execute(self._item_query() + "WHERE i.id=?", (row["id"],)).fetchone())

    def bind_launch(self, item_id: str, receipt: Any) -> None:
        with self._db() as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (_id(item_id),)).fetchone()
            if row is None or row["run_name"] != receipt.run_name:
                raise QueueError("queue_launch_uncertain")
            identity = receipt.start_identity or process_identity(receipt.pid)
            if not isinstance(receipt.pid, int) or receipt.pid <= 0 or not identity:
                raise QueueError("queue_process_unconfirmed")
            if row["pid"] and (row["pid"] != receipt.pid or row["start_identity"] != identity):
                raise QueueError("queue_launch_uncertain")
            state = (
                "cancelling"
                if row["cancel_requested"]
                else ("running" if row["status"] == "running" else "waiting_capacity")
            )
            db.execute(
                "UPDATE items SET pid=?,start_identity=?,run_dir=?,status=?,updated_at=? WHERE id=?",
                (receipt.pid, identity, receipt.run_dir, state, time.time(), item_id),
            )

    def update_item(
        self, item_id: str, *, status: str, error_code: str = "", report_ready: bool = False
    ) -> None:
        if status not in ACTIVE | TERMINAL | {"blocked"}:
            raise QueueError("queue_invalid")
        code = error_code if error_code in ERRORS else ("queue_scan_failed" if error_code else "")
        with self._db() as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (_id(item_id),)).fetchone()
            if row is None:
                raise QueueError("queue_not_found")
            if row["status"] in TERMINAL:
                return
            now = time.time()
            db.execute(
                "UPDATE items SET status=?,error_code=?,error=?,report_ready=?,updated_at=? WHERE id=?",
                (status, code or None, ERRORS.get(code), int(report_ready), now, item_id),
            )
            db.execute("UPDATE batches SET updated_at=? WHERE id=?", (now, row["batch_id"]))
            if status in TERMINAL:
                db.execute(
                    "UPDATE leases SET state='released',cleanup_complete=1,updated_at=? WHERE item_id=?",
                    (now, item_id),
                )

    def mark_stop_sent(self, item_id: str) -> None:
        with self._db() as db:
            db.execute("UPDATE items SET stop_sent=1,updated_at=? WHERE id=?", (time.time(), _id(item_id)))

    def lease_state(self, item_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM leases WHERE item_id=?", (_id(item_id),)).fetchone()
            return dict(row) if row else None

    def register_worker(
        self,
        *,
        item_id: str,
        item_token: str,
        run_name: str,
        run_dir: str,
        pid: int,
        start_identity: str,
    ) -> None:
        """Close the Popen/parent-receipt window with the child's launch proof."""
        if not start_identity or not isinstance(pid, int) or pid <= 0:
            raise QueueError("queue_process_unconfirmed")
        with self._db() as db:
            item = db.execute("SELECT * FROM items WHERE id=?", (_id(item_id),)).fetchone()
            if (
                item is None
                or item["launch_token"] != item_token
                or item["run_name"] != run_name
                or (
                    item["status"] not in ACTIVE
                    and not (item["status"] == "blocked" and item["error_code"] == "queue_launch_uncertain")
                )
            ):
                raise QueueError("queue_invalid")
            if item["pid"] and (item["pid"] != pid or item["start_identity"] != start_identity):
                raise QueueError("queue_process_unconfirmed")
            db.execute(
                "UPDATE items SET pid=?,start_identity=?,run_dir=?,status=?,error_code=NULL,error=NULL,"
                "updated_at=? WHERE id=?",
                (
                    pid,
                    start_identity,
                    run_dir,
                    "cancelling" if item["cancel_requested"] else "waiting_capacity",
                    time.time(),
                    item_id,
                ),
            )

    def fail_unstarted_orphan(self, item_id: str) -> bool:
        """Atomically revoke a dead launcher's unused child launch capability."""
        with self._db() as db:
            item = db.execute("SELECT * FROM items WHERE id=?", (_id(item_id),)).fetchone()
            if item is None or item["status"] in TERMINAL:
                return False
            if db.execute("SELECT 1 FROM leases WHERE item_id=?", (item_id,)).fetchone():
                return False
            if item["pid"]:
                dead = process_alive(item["pid"], item["start_identity"] or "") is False
            else:
                dead = process_alive(item["owner_pid"], item["owner_identity"] or "") is False
            if not dead:
                return False
            now = time.time()
            status = "cancelled" if item["cancel_requested"] else "failed"
            db.execute(
                "UPDATE items SET status=?,launch_token=NULL,error_code=?,error=?,updated_at=? WHERE id=?",
                (status, "queue_launch_failed", ERRORS["queue_launch_failed"], now, item_id),
            )
            db.execute("UPDATE batches SET updated_at=? WHERE id=?", (now, item["batch_id"]))
            return True

    def try_acquire(
        self,
        *,
        token: str,
        run_name: str,
        run_dir: str,
        pid: int,
        start_identity: str,
        item_id: str = "",
        item_token: str = "",
    ) -> bool:
        _id(token)
        if not start_identity or pid <= 0:
            raise QueueError("queue_process_unconfirmed")
        with self._db() as db:
            self._reap(db)
            item = None
            if item_id:
                item = db.execute("SELECT * FROM items WHERE id=?", (_id(item_id),)).fetchone()
                if (
                    item is None
                    or not item_token
                    or item["launch_token"] != item_token
                    or item["run_name"] != run_name
                    or item["status"] not in ACTIVE
                ):
                    raise QueueError("queue_invalid")
                if item["cancel_requested"]:
                    raise QueueError("queue_cancelled")
                if item["pid"] and (item["pid"] != pid or item["start_identity"] != start_identity):
                    raise QueueError("queue_process_unconfirmed")
            lease = db.execute("SELECT * FROM leases WHERE token=?", (token,)).fetchone()
            if lease is not None:
                if lease["pid"] != pid or lease["start_identity"] != start_identity:
                    raise QueueError("queue_invalid")
                if lease["state"] == "active":
                    return True
                if lease["state"] != "waiting":
                    raise QueueError("queue_cancelled")
            now = time.time()
            if lease is None:
                existing = db.execute(
                    "SELECT 1 FROM leases WHERE run_key=? OR item_id=?", (run_dir, item_id or None)
                ).fetchone()
                if existing:
                    raise QueueError("queue_launch_uncertain")
                db.execute(
                    "INSERT INTO leases(token,run_key,item_id,run_name,run_dir,pid,start_identity,state,"
                    "created_at,updated_at,resource_phase) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        token,
                        run_dir,
                        item_id or None,
                        run_name,
                        run_dir,
                        pid,
                        start_identity,
                        "waiting",
                        now,
                        now,
                        "not_started",
                    ),
                )
            maximum = db.execute("SELECT max_active_targets FROM queue_settings").fetchone()[0]
            occupied = self._occupied(db)
            # A batch reservation already owns this slot; insertion of its
            # waiting lease removes that reservation from _occupied atomically.
            first = db.execute(
                "SELECT token FROM leases WHERE state='waiting' ORDER BY created_at,token LIMIT 1"
            ).fetchone()
            if occupied >= maximum or (item is None and first and first["token"] != token):
                return False
            db.execute("UPDATE leases SET state='active',updated_at=? WHERE token=?", (now, token))
            if item is not None:
                db.execute(
                    "UPDATE items SET status='running',pid=?,start_identity=?,run_dir=?,updated_at=? "
                    "WHERE id=?",
                    (pid, start_identity, run_dir, now, item_id),
                )
            return True

    def finish_lease(self, token: str, *, cleanup_complete: bool, process_boundary: bool = True) -> None:
        with self._db() as db:
            row = db.execute("SELECT * FROM leases WHERE token=?", (_id(token),)).fetchone()
            if row is None or row["state"] == "released":
                return
            if row["state"] == "waiting":
                state = "released"
                cleanup_complete = True
            else:
                state = ("draining" if process_boundary else "released") if cleanup_complete else "blocked"
            db.execute(
                "UPDATE leases SET state=?,cleanup_complete=?,updated_at=? WHERE token=?",
                (state, int(cleanup_complete), time.time(), token),
            )

    def register_resource(self, token: str, *, owner: str, container: str = "", daemon: str = "") -> None:
        _id(owner)
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM leases WHERE token=? AND state='active'", (_id(token),)
            ).fetchone()
            if row is None or (row["resource_owner"] and row["resource_owner"] != owner):
                raise QueueError("queue_invalid")
            db.execute(
                "UPDATE leases SET resource_phase='started',resource_owner=?,resource_container=?,"
                "resource_daemon=?,updated_at=? WHERE token=?",
                (owner, container, daemon, time.time(), token),
            )

    def recover_orphaned_leases(self) -> int:
        """Release only after verified process death and owned resource removal."""
        path = self.path.with_suffix(self.path.suffix + ".recovery.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            return self._recover_orphaned_leases()
        finally:
            os.close(fd)

    def _recover_orphaned_leases(self) -> int:
        from .recovery import recover_resources

        with self._db() as db:
            self._reap(db)
            rows = [dict(row) for row in db.execute("SELECT * FROM leases WHERE state='blocked'")]
        recovered = 0
        for row in rows[:16]:
            if process_alive(row["pid"], row["start_identity"]) is not False:
                continue
            if not recover_resources(row):
                continue
            with self._db() as db:
                db.execute(
                    "UPDATE leases SET state='released',cleanup_complete=1,updated_at=? "
                    "WHERE token=? AND state='blocked'",
                    (time.time(), row["token"]),
                )
            recovered += 1
        return recovered
