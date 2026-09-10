"""A separate SQLite evidence store for MCP tasks, outside the scan run tree."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from strixops.traffic.scope import normalize_rules


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def valid_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError("Invalid record ID")
    return value


def storage_root() -> Path:
    return Path(os.environ.get("STRIXOPS_MCP_ROOT", "~/.strixops/mcp_tasks")).expanduser().resolve()


class TrafficStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).resolve() if root is not None else storage_root()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "traffic.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS flows(id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, created_at TEXT NOT NULL, method TEXT, host TEXT,
                    path TEXT, kind TEXT, source TEXT, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS flow_task ON flows(task_id, created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS idempotency(task_id TEXT NOT NULL, action TEXT NOT NULL,
                    key TEXT NOT NULL, record_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    PRIMARY KEY(task_id, action, key));
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create_task(
        self, name: str, allow_hosts: list[str], exclude_hosts: list[str], agent_config: dict | None = None
    ) -> dict:
        name = name.strip()
        if not name or len(name) > 160:
            raise ValueError("Task name must contain 1–160 characters")
        allow, exclude = normalize_rules(allow_hosts), normalize_rules(exclude_hosts)
        created = now()
        task = {
            "id": new_id("mcp"),
            "name": name,
            "status": "idle",
            "allow_hosts": allow,
            "exclude_hosts": exclude,
            "scope_revision": 1,
            "scope_history": {"1": {"allow_hosts": allow, "exclude_hosts": exclude}},
            "agent_config": agent_config or {},
            "created_at": created,
            "updated_at": created,
        }
        with self.connect() as db:
            db.execute("INSERT INTO tasks VALUES (?, ?)", (task["id"], json.dumps(task)))
        return task

    def get_task(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (valid_id(task_id),)).fetchone()
        if row is None:
            raise KeyError("MCP task not found")
        return json.loads(row["data"])

    def list_tasks(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT data FROM tasks ORDER BY rowid DESC").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def task_directory(self, task_id: str) -> Path:
        """Validate the complete deletion boundary without following filesystem links."""
        directory = self.root / "tasks" / valid_id(task_id)
        for path in (self.root / "tasks", directory):
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise ValueError("Refusing an unsafe MCP task storage path")
            if path.exists() and not path.is_dir():
                raise ValueError("MCP task storage path is not a directory")
        if directory.exists():
            for parent, directories, files in os.walk(directory, followlinks=False):
                for name in [*directories, *files]:
                    if (Path(parent) / name).is_symlink():
                        raise ValueError("Refusing to delete MCP task storage containing symbolic links")
        return directory

    def delete_task(self, task_id: str) -> None:
        """Remove records and stopped task storage within one database transaction.

        On a filesystem failure all database records remain available for diagnosis and
        retry. The service retains a visible delete_failed task until the purge succeeds.
        """
        directory = self.task_directory(task_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in ("sessions", "flows", "jobs", "reports", "events", "idempotency"):
                db.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            # Detect SQL failures before touching evidence files. A filesystem failure
            # rolls back these rows and leaves the task available for a cleanup retry.
            if directory.exists():
                shutil.rmtree(directory)

    def update_task(self, task_id: str, **changes) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM tasks WHERE id=?", (valid_id(task_id),)).fetchone()
            if row is None:
                raise KeyError("MCP task not found")
            task = json.loads(row["data"])
            if "allow_hosts" in changes or "exclude_hosts" in changes:
                allow = normalize_rules(changes.pop("allow_hosts", task["allow_hosts"]))
                exclude = normalize_rules(changes.pop("exclude_hosts", task["exclude_hosts"]))
                if (allow, exclude) != (task["allow_hosts"], task["exclude_hosts"]):
                    revision = int(task["scope_revision"]) + 1
                    task.update(allow_hosts=allow, exclude_hosts=exclude, scope_revision=revision)
                    task["scope_history"][str(revision)] = {"allow_hosts": allow, "exclude_hosts": exclude}
            if "name" in changes:
                changes["name"] = str(changes["name"]).strip()
                if not changes["name"] or len(changes["name"]) > 160:
                    raise ValueError("Task name must contain 1–160 characters")
            task.update(changes, updated_at=now())
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task), task_id))
        return task

    def put_session(self, session: dict) -> dict:
        with self.connect() as db:
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (session["id"], session["task_id"], json.dumps(session)),
            )
        return session

    def sessions(self, task_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT data FROM sessions WHERE task_id=? ORDER BY rowid DESC", (valid_id(task_id),)
            ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def put_flow(self, task_id: str, flow: dict, session_id: str = "") -> dict:
        flow = dict(flow)
        session_id = str(flow.get("session_id") or session_id or "replay")
        native_id = str(flow.get("id") or new_id("flow"))
        # Capture IDs are stable across request/response updates, isolated per session.
        flow_id = (
            native_id
            if native_id.startswith("flow_")
            else "flow_" + hashlib.sha256((session_id + ":" + native_id).encode()).hexdigest()[:40]
        )
        request = flow.get("request") or {}
        response = flow.get("response") or {}
        parsed = urlsplit(str(flow.get("url") or request.get("url") or ""))
        flow.update(
            id=flow_id,
            task_id=task_id,
            session_id=session_id,
            method=flow.get("method") or request.get("method") or "GET",
            url=flow.get("url") or request.get("url") or "",
            host=parsed.hostname or "",
            path=parsed.path or "/",
            status_code=flow.get("status_code") or response.get("status_code"),
            kind=flow.get("kind") or "other",
            source=flow.get("source") or "user",
            created_at=flow.get("created_at") or now(),
        )
        with self.connect() as db:
            existing = db.execute("SELECT task_id FROM flows WHERE id=?", (flow_id,)).fetchone()
            if existing and existing["task_id"] != task_id:
                raise ValueError("Flow belongs to another task")
            db.execute(
                """INSERT INTO flows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET method=excluded.method,host=excluded.host,
                path=excluded.path,kind=excluded.kind,source=excluded.source,data=excluded.data""",
                (
                    flow_id,
                    task_id,
                    session_id,
                    flow["created_at"],
                    flow["method"],
                    flow["host"],
                    flow["path"],
                    flow["kind"],
                    flow["source"],
                    json.dumps(flow),
                ),
            )
        return flow

    def get_flow(self, task_id: str, flow_id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT data FROM flows WHERE task_id=? AND id=?", (valid_id(task_id), valid_id(flow_id))
            ).fetchone()
        if row is None:
            raise KeyError("Request not found in this MCP task")
        return json.loads(row["data"])

    def flows(
        self, task_id: str, *, q: str = "", kind: str = "", source: str = "", after: int = 0, limit: int = 50
    ) -> dict:
        conditions, args = ["task_id=?"], [valid_id(task_id)]
        if q:
            conditions.append("(instr(lower(host || path || method), lower(?))>0)")
            args.append(q[:500])
        for field, value in (("kind", kind), ("source", source)):
            if value:
                conditions.append(field + "=?")
                args.append(value)
        where = " AND ".join(conditions)
        limit = max(1, min(int(limit), 200))
        after = max(0, int(after))
        with self.connect() as db:
            total = db.execute("SELECT count(*) FROM flows WHERE " + where, args).fetchone()[0]
            if after:
                where += " AND rowid < ?"
                args.append(after)
            rows = db.execute(
                "SELECT rowid,data FROM flows WHERE " + where + " ORDER BY rowid DESC LIMIT ?",
                [*args, limit + 1],
            ).fetchall()
        page = rows[:limit]
        return {
            "flows": [json.loads(row[1]) for row in page],
            "total": total,
            "next_cursor": str(page[-1][0]) if len(rows) > limit else None,
        }

    def endpoints(self, task_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,method,host,path,kind,created_at FROM flows WHERE task_id=? "
                "ORDER BY created_at DESC",
                (valid_id(task_id),),
            ).fetchall()
        groups = {}
        for row in rows:
            path = re.sub(r"(?<=/)(?:\d+|[a-fA-F0-9]{24,}|[a-fA-F0-9-]{36})(?=/|$)", "{id}", row["path"])
            key = (row["method"], row["host"], path, row["kind"])
            if key not in groups:
                groups[key] = {
                    "id": hashlib.sha256(repr(key).encode()).hexdigest()[:20],
                    "method": key[0],
                    "host": key[1],
                    "path": path,
                    "kind": key[3],
                    "count": 0,
                    "flow_count": 0,
                    "last_seen": row["created_at"],
                    "latest_flow_id": row["id"],
                    "inferred": path != row["path"],
                }
            groups[key]["count"] += 1
            groups[key]["flow_count"] += 1
        return list(groups.values())

    def put_record(self, table: str, task_id: str, record: dict) -> dict:
        if table not in {"jobs", "reports"}:
            raise ValueError("Invalid record type")
        with self.connect() as db:
            previous = db.execute(f"SELECT task_id FROM {table} WHERE id=?", (record["id"],)).fetchone()
            if previous and previous[0] != task_id:
                raise ValueError("Record belongs to another task")
            if table == "reports" and previous:
                raise ValueError("Report versions are immutable")
            db.execute(
                f"INSERT INTO {table} VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (record["id"], task_id, record["created_at"], json.dumps(record)),
            )
        return record

    def records(self, table: str, task_id: str) -> list[dict]:
        if table not in {"jobs", "reports"}:
            raise ValueError("Invalid record type")
        with self.connect() as db:
            rows = db.execute(
                f"SELECT data FROM {table} WHERE task_id=? ORDER BY created_at DESC", (valid_id(task_id),)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def record(self, table: str, task_id: str, record_id: str) -> dict:
        if table not in {"jobs", "reports"}:
            raise ValueError("Invalid record type")
        with self.connect() as db:
            row = db.execute(
                f"SELECT data FROM {table} WHERE task_id=? AND id=?", (valid_id(task_id), valid_id(record_id))
            ).fetchone()
        if row is None:
            raise KeyError("Record not found in this MCP task")
        return json.loads(row[0])

    def counts(self, task_id: str) -> dict:
        with self.connect() as db:
            rows = db.execute(
                "SELECT kind,count(*) FROM flows WHERE task_id=? GROUP BY kind", (task_id,)
            ).fetchall()
        counts = dict(rows)
        jobs = self.records("jobs", task_id)
        return {
            "flows": sum(counts.values()),
            "pages": counts.get("page", 0),
            "apis": counts.get("api", 0),
            "tests": len(jobs),
            "findings": sum(len((job.get("result") or {}).get("findings", [])) for job in jobs),
        }

    def event(self, task_id: str, event: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(task_id,data) SELECT ?,? WHERE EXISTS(SELECT 1 FROM tasks WHERE id=?)",
                (task_id, json.dumps({"created_at": now(), **event}), task_id),
            )
            db.execute(
                "DELETE FROM events WHERE task_id=? AND seq < (SELECT max(seq)-10000 FROM events)", (task_id,)
            )

    def events(self, task_id: str, after: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT seq,data FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 100",
                (valid_id(task_id), after),
            ).fetchall()
        return [{"id": row[0], **json.loads(row[1])} for row in rows]

    def idempotent(self, task_id: str, action: str, key: str, body: dict, record_id: str = "") -> str:
        if not key:
            return ""
        if len(key) > 128:
            raise ValueError("Idempotency key is too long")
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            row = db.execute(
                "SELECT record_id,fingerprint FROM idempotency WHERE task_id=? AND action=? AND key=?",
                (task_id, action, key),
            ).fetchone()
            if row:
                if row[1] != fingerprint:
                    raise ValueError("Idempotency key was already used with different input")
                return row[0]
            if record_id:
                db.execute(
                    "INSERT INTO idempotency VALUES (?,?,?,?,?)",
                    (task_id, action, key, record_id, fingerprint),
                )
        return ""

    def complete_idempotent(self, task_id: str, action: str, key: str, record_id: str) -> None:
        if key:
            with self.connect() as db:
                db.execute(
                    "UPDATE idempotency SET record_id=? WHERE task_id=? AND action=? AND key=?",
                    (record_id, task_id, action, key),
                )
