"""Independent SQLite search history and immutable scan-draft snapshots."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from strixops.console.project_scope import TargetValidationError, normalize_target
from strixops.engine.targets import normalize_targets

from .client import FIELDS
from .errors import FofaError
from .settings import checked_file, checked_root, lock

FILTER_FIELDS = frozenset(FIELDS) | {"q", "web_only"}
SORT_FIELDS = frozenset(FIELDS) | {"position"}
SEARCH_FIELDS = (
    "search_id,query,status,created_at,updated_at,max_results,loaded_count,"
    "total_available,pages_fetched,error_code,limited"
)
RESULT_FIELDS = "result_id,position," + ",".join(FIELDS) + ",web_target,selectable,selection_reason"


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier(value: object, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(prefix + r"-[0-9a-f]{32}", value):
        raise FofaError("not_found", 404)
    return value


def integer(value: object, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value):
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise FofaError("invalid_request")
    return value


def filters(values: dict) -> dict:
    if not isinstance(values, dict) or set(values) - FILTER_FIELDS:
        raise FofaError("invalid_request")
    result = {}
    for field, value in values.items():
        if value is None or value == "":
            continue
        if field == "web_only":
            if value in ("true", "false"):
                value = value == "true"
            if type(value) is not bool:
                raise FofaError("invalid_request")
        elif field == "port":
            value = integer(value, 0, 1, 65535)
        elif not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise FofaError("invalid_request")
        result[field] = value
    return result


def order(sort: str = "position", direction: str = "asc") -> str:
    if sort not in SORT_FIELDS or direction not in {"asc", "desc"}:
        raise FofaError("invalid_request")
    return f"{sort} {direction}, position asc"


def _where(search_id: str, selected_filters: dict) -> tuple[str, list]:
    terms, params = ["search_id = ?"], [identifier(search_id, "search")]
    for field, value in filters(selected_filters).items():
        if field == "web_only":
            if value:
                terms.append("selectable = 1")
        elif field in {"port", "protocol", "country"}:
            terms.append(f"{field} = ? COLLATE NOCASE")
            params.append(value)
        else:
            escaped = "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            if field == "q":
                terms.append("(" + " OR ".join(f"{name} LIKE ? ESCAPE '\\'" for name in FIELDS) + ")")
                params.extend([escaped] * len(FIELDS))
            else:
                terms.append(f"{field} LIKE ? ESCAPE '\\'")
                params.append(escaped)
    return " AND ".join(terms), params


def web_target(host: str, protocol: str, port: int | None) -> tuple[str | None, str | None]:
    """Preserve explicit web URLs and ports; never infer a protocol from its port."""
    if len(host) > 8192 or any(c.isspace() or ord(c) < 32 for c in host) or "\\" in host:
        return None, "invalid_target"
    explicit = "://" in host
    if not explicit and protocol.lower() not in {"http", "https"}:
        return None, "unsupported_protocol"
    try:
        parsed = urlsplit(host if explicit else protocol.lower() + "://" + host)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None, "unsupported_protocol"
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port == 0
        ):
            return None, "invalid_target"
        # A service port supplied separately still belongs to the target. Never
        # turn https://host + port 8443 into an implicit connection to 443.
        netloc = parsed.netloc
        if parsed.port is None and port is not None:
            netloc += f":{port}"
        elif parsed.port is not None and port is not None and parsed.port != port:
            return None, "invalid_target"
        target = urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, ""))
        # Reuse admission's pure syntax/length checks without replacing the URL
        # by its host-only scope representation or probing the discovered host.
        normalize_targets(target)
        normalize_target(target, "web")
        return target, None
    except (ValueError, UnicodeError, TargetValidationError):
        return None, "invalid_target"


def prepare_row(values: list, position: int, key: str) -> dict:
    row = dict(zip(FIELDS, [str(value) for value in values], strict=True))
    invalid_host = "\x00" in row["host"]
    secret_in_host = bool(key and (key in row["host"] or quote(key, safe="") in row["host"]))
    for field, value in row.items():
        if key:
            value = value.replace(key, "[redacted]").replace(quote(key, safe=""), "[redacted]")
        row[field] = value.replace("\x00", "")
    raw_port = row["port"]
    row["port"] = (
        int(raw_port) if re.fullmatch(r"[0-9]{1,5}", raw_port) and 1 <= int(raw_port) <= 65535 else None
    )
    target, reason = web_target(row["host"], row["protocol"], row["port"])
    if secret_in_host or invalid_host or (raw_port and row["port"] is None):
        target, reason = None, "invalid_target"
    return {
        "result_id": "result-" + uuid.uuid4().hex,
        "position": position,
        **row,
        "web_target": target,
        "selectable": bool(target),
        "selection_reason": reason,
    }


def _public(row: sqlite3.Row) -> dict:
    result = dict(row)
    for key in ("limited", "selectable"):
        if key in result:
            result[key] = bool(result[key])
    return result


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "history.sqlite3"

    @contextmanager
    def connect(self, *, write: bool = False):
        connection = None
        try:
            exists = checked_root(self.root, create=write) and checked_file(self.path, missing=True)
            if not exists and not write:
                yield None
                return
            # Journal files inherit the private database permissions. Refuse
            # preexisting links instead of asking SQLite to follow them.
            for suffix in ("-journal", "-wal", "-shm"):
                checked_file(Path(str(self.path) + suffix), missing=True)
            if write and not exists:
                with lock(self.root, ".database.lock"):
                    fd = (
                        os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                        if not self.path.exists()
                        else None
                    )
                    if fd is not None:
                        os.close(fd)
                    checked_file(self.path)
                    with sqlite3.connect(self.path, timeout=5) as setup:
                        setup.executescript("""
                            CREATE TABLE IF NOT EXISTS searches (
                              search_id TEXT PRIMARY KEY, query TEXT NOT NULL, status TEXT NOT NULL,
                              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                              max_results INTEGER NOT NULL,
                              loaded_count INTEGER NOT NULL DEFAULT 0, total_available INTEGER,
                              pages_fetched INTEGER NOT NULL DEFAULT 0, error_code TEXT,
                              limited INTEGER NOT NULL DEFAULT 0);
                            CREATE TABLE IF NOT EXISTS results (
                              result_id TEXT PRIMARY KEY, search_id TEXT NOT NULL
                                REFERENCES searches(search_id) ON DELETE CASCADE,
                              position INTEGER NOT NULL, host TEXT, ip TEXT, port INTEGER, protocol TEXT,
                              domain TEXT, title TEXT, country TEXT,
                              region TEXT, city TEXT, server TEXT,
                              lastupdatetime TEXT, web_target TEXT,
                              selectable INTEGER, selection_reason TEXT);
                            CREATE INDEX IF NOT EXISTS result_search ON results(search_id,position);
                            CREATE TABLE IF NOT EXISTS drafts (
                              draft_id TEXT PRIMARY KEY, search_id TEXT NOT NULL,
                              created_at TEXT NOT NULL, targets TEXT NOT NULL);
                            PRAGMA user_version=1;
                        """)
            mode = "rw" if write else "ro"
            connection = sqlite3.connect(self.path.absolute().as_uri() + f"?mode={mode}", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise FofaError("storage_unavailable", 503)
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except (sqlite3.Error, OSError, ValueError):
            raise FofaError("storage_unavailable", 503) from None
        finally:
            if connection is not None:
                connection.close()

    def create_search(self, query: str, max_results: int) -> dict:
        stamp, search_id = now(), "search-" + uuid.uuid4().hex
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO searches(search_id,query,status,created_at,updated_at,max_results) "
                "VALUES(?,?,'queued',?,?,?)",
                (search_id, query, stamp, stamp, max_results),
            )
        return self.search(search_id)

    def searches(self, limit: int = 20, offset: int = 0) -> dict:
        with self.connect() as db:
            rows = (
                []
                if db is None
                else [
                    _public(row)
                    for row in db.execute(
                        f"SELECT {SEARCH_FIELDS} FROM searches "
                        "ORDER BY created_at DESC,search_id LIMIT ? OFFSET ?",
                        (limit, offset),
                    )
                ]
            )
            total = 0 if db is None else db.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
        return {"searches": rows, "total": total, "limit": limit, "offset": offset}

    def search(self, search_id: str) -> dict:
        identifier(search_id, "search")
        with self.connect() as db:
            row = (
                None
                if db is None
                else db.execute(
                    f"SELECT {SEARCH_FIELDS} FROM searches WHERE search_id=?", (search_id,)
                ).fetchone()
            )
        if row is None:
            raise FofaError("not_found", 404)
        return _public(row)

    def status(self, search_id: str, status: str, code: str | None = None):
        with self.connect(write=True) as db:
            db.execute(
                "UPDATE searches SET status=?,error_code=?,updated_at=? WHERE search_id=?",
                (status, code, now(), search_id),
            )
            if status == "completed":
                db.execute(
                    "UPDATE searches SET limited=1 WHERE search_id=? "
                    "AND total_available IS NULL AND loaded_count>=max_results",
                    (search_id,),
                )

    def recover(self):
        if not checked_root(self.root) or not checked_file(self.path, missing=True):
            return
        with self.connect(write=True) as db:
            db.execute(
                "UPDATE searches SET status=CASE WHEN loaded_count>0 THEN 'partial' ELSE 'failed' END,"
                "error_code='interrupted',updated_at=? WHERE status IN ('queued','running')",
                (now(),),
            )

    def append_page(self, search_id: str, rows: list[dict], total: int | None):
        with self.connect(write=True) as db:
            for row in rows:
                columns = ("search_id", *row.keys())
                db.execute(
                    f"INSERT INTO results ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    (search_id, *row.values()),
                )
            db.execute(
                "UPDATE searches SET loaded_count=loaded_count+?,pages_fetched=pages_fetched+1,"
                "total_available=COALESCE(?,total_available),updated_at=?,"
                "limited=CASE WHEN ? IS NOT NULL AND ?>max_results THEN 1 ELSE limited END "
                "WHERE search_id=?",
                (len(rows), total, now(), total, total, search_id),
            )

    def results(
        self,
        search_id: str,
        selected_filters: dict,
        *,
        sort: str = "position",
        direction: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        search = self.search(search_id)
        where, params = _where(search_id, selected_filters)
        ordering = order(sort, direction)
        with self.connect() as db:
            count = db.execute(f"SELECT COUNT(*) FROM results WHERE {where}", params).fetchone()[0]
            rows = [
                _public(row)
                for row in db.execute(
                    f"SELECT {RESULT_FIELDS} FROM results WHERE {where} ORDER BY {ordering} LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                )
            ]
        return {
            "results": rows,
            "total": count,
            "loaded_count": search["loaded_count"],
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < count,
        }

    def delete(self, search_id: str):
        self.search(search_id)
        with self.connect(write=True) as db:
            db.execute("DELETE FROM searches WHERE search_id=?", (search_id,))

    def create_draft(self, search_id: str, body: dict) -> dict:
        if set(body) - {"result_ids", "all_matching", "filters"}:
            raise FofaError("invalid_request")
        if body.get("all_matching") is True and "result_ids" not in body:
            rows = self.results(search_id, filters(body.get("filters", {})), limit=101)["results"]
        elif (
            isinstance(body.get("result_ids"), list)
            and not body.get("all_matching")
            and not body.get("filters")
        ):
            ids = body["result_ids"]
            if not 1 <= len(ids) <= 100 or len(set(str(i) for i in ids)) != len(ids):
                raise FofaError("selection_limit")
            for item in ids:
                identifier(item, "result")
            self.search(search_id)
            with self.connect() as db:
                rows = [
                    _public(row)
                    for row in db.execute(
                        f"SELECT {RESULT_FIELDS} FROM results WHERE search_id=? "
                        f"AND result_id IN ({','.join('?' for _ in ids)}) ORDER BY position",
                        (search_id, *ids),
                    )
                ]
            if len(rows) != len(ids):
                raise FofaError("not_found", 404)
        else:
            raise FofaError("invalid_request")
        if not 1 <= len(rows) <= 100:
            raise FofaError("selection_limit")
        if any(not row["selectable"] or not row["web_target"] for row in rows):
            raise FofaError("unsupported_targets")
        targets = [
            {
                "target": row["web_target"],
                "source": {"kind": "fofa", "search_id": search_id, "result_id": row["result_id"]},
            }
            for row in rows
        ]
        draft_id, stamp = "draft-" + uuid.uuid4().hex, now()
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO drafts VALUES(?,?,?,?)",
                (draft_id, search_id, stamp, json.dumps(targets, ensure_ascii=False)),
            )
        return {"draft_id": draft_id, "target_count": len(targets)}

    def draft(self, draft_id: str) -> dict:
        identifier(draft_id, "draft")
        with self.connect() as db:
            row = (
                None
                if db is None
                else db.execute(
                    "SELECT draft_id,search_id,created_at,targets FROM drafts WHERE draft_id=?", (draft_id,)
                ).fetchone()
            )
        if row is None:
            raise FofaError("not_found", 404)
        try:
            targets = json.loads(row["targets"])
            if not isinstance(targets, list) or not 1 <= len(targets) <= 100:
                raise ValueError
            clean = []
            for item in targets:
                target = item["target"]
                source = item["source"]
                if (
                    not isinstance(target, str)
                    or web_target(target, "", None)[0] != target
                    or source["kind"] != "fofa"
                    or source["search_id"] != row["search_id"]
                ):
                    raise ValueError
                clean.append(
                    {
                        "target": target,
                        "source": {
                            "kind": "fofa",
                            "search_id": identifier(source["search_id"], "search"),
                            "result_id": identifier(source["result_id"], "result"),
                        },
                    }
                )
            return {
                "draft_id": draft_id,
                "search_id": row["search_id"],
                "created_at": row["created_at"],
                "targets": clean,
                "target_count": len(clean),
            }
        except (KeyError, TypeError, ValueError, RecursionError, FofaError):
            raise FofaError("storage_unavailable", 503) from None
