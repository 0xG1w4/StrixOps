"""Run-owned credential register with attributed, revisioned, atomic updates.

Agents record exact observations here; the unified inventory generates CSV.
Every mutation reloads under a process-safe file lock, while readers never
create files. Console readers may use the pure parser and projection helpers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strixops.report.credential_database import CredentialDatabase, DatabaseError

MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_CREDENTIALS = 5000
MAX_HISTORY = 10
MAX_SOURCES = 100
VALIDATION_STATUSES = ("unverified", "validated", "failed", "unknown")
SECRET_TYPES = ("password", "hash", "api_key", "token", "secret", "private_key", "encryption_key", "username")
_SEVERITIES = ("", "info", "low", "medium", "high", "critical")
_IDENTITY = ("host", "username", "password", "hash", "secret_type")
_LIMITS = {
    "host": 2048,
    "username": 2048,
    "password": 32768,
    "hash": 32768,
    "source": 4096,
    "note": 8192,
    "validation_evidence": 8192,
}
_ID = re.compile(r"cred-[0-9a-f]{20}\Z")
_VERSION_KEYS = set(_LIMITS) | {
    "id",
    "secret_type",
    "severity",
    "validation_status",
    "sources",
    "revision",
    "created_by",
    "updated_by",
    "created_at",
    "updated_at",
}
_MESSAGES = {
    "invalid_arguments": "Invalid credential arguments; check the tool fields and limits.",
    "invalid_dataset": "The credential dataset is invalid; no records were imported.",
    "cancelled": "Credential import was cancelled; no records were imported.",
    "credential_not_found": "The credential was not found in this run.",
    "revision_conflict": "The credential changed; read its current revision before updating it.",
    "content_limit": "The credential exceeds a field, provenance or revision limit.",
    "credential_limit": "This run has reached its credential count limit.",
    "store_limit": "The credential register exceeds its storage limit.",
    "invalid_store": "The saved credential register is invalid; it has been preserved unchanged.",
    "unsafe_storage": "Credential storage is not a regular run-owned file or directory.",
    "storage_unavailable": "Credential storage is unavailable or changed during this operation.",
    "internal_error": "Credentials could not be processed; continue the task using the available evidence.",
}


class CredentialError(ValueError):
    def __init__(self, code: str, *, current_revision: int | None = None) -> None:
        self.code = code if code in _MESSAGES else "internal_error"
        self.current_revision = current_revision
        super().__init__(_MESSAGES[self.code])


def error_result(code: str, *, current_revision: int | None = None) -> dict:
    error = CredentialError(code, current_revision=current_revision)
    result = {"success": False, "continue_task": True, "error_code": error.code, "error": str(error)}
    if type(current_revision) is int and current_revision > 0:
        result["current_revision"] = current_revision
    return result


def _success(**fields: Any) -> dict:
    return {"success": True, "continue_task": True, **fields}


def empty_document() -> dict:
    return {"schema_version": 1, "credentials": []}


def _text(value: Any, limit: int, *, empty: bool = True) -> str:
    if not isinstance(value, str) or "\x00" in value or (not empty and not value.strip()):
        raise CredentialError("invalid_arguments")
    if len(value) > limit:
        raise CredentialError("content_limit")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CredentialError("invalid_arguments") from None
    return value


def _choice(value: Any, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise CredentialError("invalid_arguments")
    return value


def _author(agent_id: Any, agent_name: Any) -> dict:
    return {"agent_id": _text(agent_id, 128, empty=False), "agent_name": _text(agent_name, 200, empty=False)}


def _revision(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000_000:
        raise CredentialError("invalid_arguments")
    return value


def _id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CredentialError("invalid_arguments")
    return value


def _credential_id(row: dict) -> str:
    # Deliberately identical to credentials.merge_credentials' identity encoding.
    identity = tuple(row[field] for field in _IDENTITY)
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]
    return f"cred-{digest}"


def _provenance(row: dict, author: dict, source: str) -> dict:
    return {
        "kind": "credential_register",
        "id": row["id"],
        "title": author["agent_name"],
        **author,
        "source": source,
    }


def _validate_version(row: Any) -> None:
    if not isinstance(row, dict) or set(row) != _VERSION_KEYS:
        raise CredentialError("invalid_store")
    for key, limit in _LIMITS.items():
        _text(row[key], limit)
    _choice(row["secret_type"], SECRET_TYPES)
    _choice(row["severity"], _SEVERITIES)
    _choice(row["validation_status"], VALIDATION_STATUSES)
    if row["secret_type"] == "username":
        if not row["username"].strip() or row["password"] or row["hash"]:
            raise CredentialError("invalid_store")
    elif row["secret_type"] == "hash":
        if not row["hash"] or row["password"]:
            raise CredentialError("invalid_store")
    elif row["hash"] or (
        not row["password"] and not (row["secret_type"] == "password" and row["username"].strip())
    ):
        raise CredentialError("invalid_store")
    if row["validation_status"] in {"validated", "failed"} and not row["validation_evidence"].strip():
        raise CredentialError("invalid_store")
    if _id(row["id"]) != _credential_id(row):
        raise CredentialError("invalid_store")
    _revision(row["revision"])
    for key in ("created_by", "updated_by"):
        author = row[key]
        if not isinstance(author, dict) or set(author) != {"agent_id", "agent_name"}:
            raise CredentialError("invalid_store")
        _author(**author)
    for key in ("created_at", "updated_at"):
        value = _text(row[key], 64, empty=False)
        if datetime.fromisoformat(value).tzinfo is None:
            raise CredentialError("invalid_store")
    sources = row["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise CredentialError("invalid_store")
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "kind",
            "id",
            "title",
            "agent_id",
            "agent_name",
            "source",
        }:
            raise CredentialError("invalid_store")
        author = _author(source["agent_id"], source["agent_name"])
        _text(source["source"], _LIMITS["source"])
        if source != _provenance(row, author, source["source"]):
            raise CredentialError("invalid_store")
        encoded = json.dumps(source, sort_keys=True)
        if encoded in seen:
            raise CredentialError("invalid_store")
        seen.add(encoded)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CredentialError("invalid_store")
        result[key] = value
    return result


def parse_document(raw: bytes) -> dict:
    """Validate untrusted bytes, with fixed errors that never echo secrets."""
    if not isinstance(raw, bytes):
        raise CredentialError("invalid_store")
    if len(raw) > MAX_STORE_BYTES:
        raise CredentialError("store_limit")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(document, dict) or set(document) != {"schema_version", "credentials"}:
            raise CredentialError("invalid_store")
        if type(document["schema_version"]) is not int or document["schema_version"] != 1:
            raise CredentialError("invalid_store")
        rows = document["credentials"]
        if not isinstance(rows, list) or len(rows) > MAX_CREDENTIALS:
            raise CredentialError("invalid_store")
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != _VERSION_KEYS | {"history", "history_truncated"}:
                raise CredentialError("invalid_store")
            _validate_version({key: row[key] for key in _VERSION_KEYS})
            if row["id"] in seen:
                raise CredentialError("invalid_store")
            seen.add(row["id"])
            history = row["history"]
            if not isinstance(history, list) or len(history) > MAX_HISTORY:
                raise CredentialError("invalid_store")
            start = row["revision"] - len(history)
            if start < 1 or type(row["history_truncated"]) is not bool:
                raise CredentialError("invalid_store")
            if row["history_truncated"] != (start > 1):
                raise CredentialError("invalid_store")
            for revision, prior in enumerate(history, start):
                _validate_version(prior)
                if (
                    prior["id"] != row["id"]
                    or prior["revision"] != revision
                    or prior["created_by"] != row["created_by"]
                    or prior["created_at"] != row["created_at"]
                    or any(prior[key] != row[key] for key in _IDENTITY)
                ):
                    raise CredentialError("invalid_store")
        return document
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, OverflowError):
        raise CredentialError("invalid_store") from None


def project_snapshot(document: dict) -> dict:
    """Project current rows only; validation evidence and attribution stay intact."""
    return _success(
        credentials=[
            {key: copy.deepcopy(row[key]) for key in _VERSION_KEYS} for row in document["credentials"]
        ],
        source_status="available",
    )


def _find(document: dict, credential_id: str) -> dict:
    _id(credential_id)
    for row in document["credentials"]:
        if row["id"] == credential_id:
            return row
    raise CredentialError("credential_not_found")


def _current(row: dict, *, include_history: bool = False) -> dict:
    result = copy.deepcopy(row)
    if not include_history:
        result.pop("history")
    return result


def _fingerprint(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class CredentialStore:
    """Queryable run register; bulk imports use one disk-backed transaction."""

    def __init__(self, run_dir: Path, lock: Any = None) -> None:
        self.run_dir = Path(run_dir).absolute()
        # Accept the old shared-lock argument without blocking run activity.
        self._database = CredentialDatabase(self.run_dir)

    def _load_legacy(self, directory: int | None) -> tuple[dict, str]:
        if directory is None:
            return empty_document(), "missing"
        info = self._database.safe_file(directory, "credentials.json")
        if info is None:
            return empty_document(), "missing"
        if info.st_size > MAX_STORE_BYTES:
            raise CredentialError("store_limit")
        descriptor = os.open(
            "credentials.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise CredentialError("unsafe_storage")
            if _fingerprint(opened) != _fingerprint(info):
                raise CredentialError("storage_unavailable")
            raw = stream.read(MAX_STORE_BYTES + 1)
            current = self._database.safe_file(directory, "credentials.json")
            if current is None or _fingerprint(current) != _fingerprint(info) or len(raw) != info.st_size:
                raise CredentialError("storage_unavailable")
        return parse_document(raw), "available"

    @contextmanager
    def _transaction(self, *, write: bool = False, cancelled=None):
        with self._database.transaction(write=write, cancelled=cancelled) as (connection, fresh, directory):
            if fresh:
                legacy, _ = self._load_legacy(directory)
                for row in legacy["credentials"]:
                    self._put(connection, row)
            yield connection, directory

    def _invoke(self, operation: str, **kwargs: Any) -> dict:
        try:
            # Never hold RunState's shared lock during bulk imports or reads.
            # SQLite and the database's writer lock own credential serialization.
            return getattr(self, f"_{operation}")(**kwargs)
        except CredentialError as exc:
            return error_result(exc.code, current_revision=exc.current_revision)
        except DatabaseError as exc:
            return error_result(exc.code)
        except OSError:
            return error_result("storage_unavailable")
        except Exception:
            return error_result("internal_error")

    @staticmethod
    def _put(connection, row: dict) -> None:
        fields = ("id", "host", "username", "secret_type", "validation_status", "severity", "updated_at")
        values = [row[field] for field in fields]
        values.extend(
            [
                int(row["validation_status"] == "validated"),
                _SEVERITIES.index(row["severity"]),
                "\n".join(row[field] for field in _LIMITS).casefold(),
                json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            ]
        )
        connection.execute(
            "INSERT INTO credentials (id,host,username,secret_type,validation_status,severity,updated_at,"
            "validation_rank,severity_rank,search_text,record) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET host=excluded.host,username=excluded.username,"
            "secret_type=excluded.secret_type,validation_status=excluded.validation_status,"
            "severity=excluded.severity,updated_at=excluded.updated_at,validation_rank=excluded.validation_rank,"
            "severity_rank=excluded.severity_rank,search_text=excluded.search_text,record=excluded.record",
            values,
        )

    @staticmethod
    def _decode(raw: str) -> dict:
        if not isinstance(raw, str) or len(raw) > MAX_STORE_BYTES:
            raise CredentialError("invalid_store")
        wrapped = ('{"schema_version":1,"credentials":[' + raw + "]}").encode("utf-8")
        return parse_document(wrapped)["credentials"][0]

    def _fetch(self, connection, credential_id: str) -> dict | None:
        saved = connection.execute("SELECT record FROM credentials WHERE id=?", (credential_id,)).fetchone()
        if saved is None:
            return None
        row = self._decode(saved[0])
        if row["id"] != credential_id:
            raise CredentialError("invalid_store")
        return row

    @staticmethod
    def _filters(query=None, validation_status=None, exclude_ids=()) -> tuple[str, list, set[str]]:
        if query is not None:
            query = _text(query, 500).casefold()
        if validation_status is not None:
            _choice(validation_status, VALIDATION_STATUSES)
        excluded = {_id(value) for value in exclude_ids}
        clauses, values = [], []
        if query:
            clauses.append("instr(search_text,?)>0")
            values.append(query)
        if validation_status is not None:
            clauses.append("validation_status=?")
            values.append(validation_status)
        if excluded:
            clauses.append("id NOT IN (SELECT value FROM json_each(?))")
            values.append(json.dumps(sorted(excluded)))
        return " WHERE " + " AND ".join(clauses) if clauses else "", values, excluded

    @staticmethod
    def _summary(connection, *, exclude_ids=()) -> tuple[int, dict]:
        where, values, _ = CredentialStore._filters(exclude_ids=exclude_ids)
        summary = {
            "validation_status": dict.fromkeys(VALIDATION_STATUSES, 0),
            "secret_type": dict.fromkeys(SECRET_TYPES, 0),
            "severity": dict.fromkeys(_SEVERITIES, 0),
        }
        total = connection.execute("SELECT COUNT(*) FROM credentials" + where, values).fetchone()[0]
        for field in summary:
            for key, count in connection.execute(
                f"SELECT {field},COUNT(*) FROM credentials" + where + f" GROUP BY {field}", values
            ):
                if key not in summary[field]:
                    raise CredentialError("invalid_store")
                summary[field][key] = count
        return total, summary

    @staticmethod
    def _legacy_summary(rows: list[dict]) -> dict:
        summary = {
            "validation_status": dict.fromkeys(VALIDATION_STATUSES, 0),
            "secret_type": dict.fromkeys(SECRET_TYPES, 0),
            "severity": dict.fromkeys(_SEVERITIES, 0),
        }
        for row in rows:
            for field in summary:
                summary[field][row[field]] += 1
        return summary

    @staticmethod
    def _datasets(connection, *, limit: int | None = 100, ids=(), paths=()) -> list[dict]:
        ids, paths = list(ids), list(paths)
        if any(
            not isinstance(value, str) or not re.fullmatch(r"dataset-[0-9a-f]{32}", value) for value in ids
        ):
            raise CredentialError("invalid_arguments")
        for path in paths:
            _text(path, 4096)
        clauses, values = [], []
        if ids:
            clauses.append("id IN (SELECT value FROM json_each(?))")
            values.append(json.dumps(ids))
        if paths:
            clauses.append("path IN (SELECT value FROM json_each(?))")
            values.append(json.dumps(paths))
        sql = "SELECT record FROM credential_datasets"
        if clauses:
            sql += " WHERE " + " OR ".join(clauses)
        sql += " ORDER BY created_at DESC,id"
        if limit is not None:
            sql += " LIMIT ?"
            values.append(limit)
        rows = []
        for (raw,) in connection.execute(sql, values):
            if not isinstance(raw, str) or len(raw) > 16384:
                raise CredentialError("invalid_store")
            try:
                row = json.loads(raw, object_pairs_hook=_unique_object)
                if not isinstance(row, dict) or set(row) != {
                    "id",
                    "source",
                    "path",
                    "rows_read",
                    "inserted",
                    "duplicates",
                    "created_by",
                    "created_at",
                    "sha256",
                    "size",
                }:
                    raise CredentialError("invalid_store")
                if not re.fullmatch(r"dataset-[0-9a-f]{32}", row["id"]):
                    raise CredentialError("invalid_store")
                for field in ("rows_read", "inserted", "duplicates"):
                    if type(row[field]) is not int or row[field] < 0:
                        raise CredentialError("invalid_store")
                if row["inserted"] + row["duplicates"] != row["rows_read"]:
                    raise CredentialError("invalid_store")
                _text(row["source"], 4096)
                _text(row["path"], 4096)
                if row["sha256"] and not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
                    raise CredentialError("invalid_store")
                if not isinstance(row["sha256"], str) or type(row["size"]) is not int or row["size"] < 0:
                    raise CredentialError("invalid_store")
                _author(**row["created_by"])
                if datetime.fromisoformat(row["created_at"]).tzinfo is None:
                    raise CredentialError("invalid_store")
            except (TypeError, ValueError, KeyError):
                raise CredentialError("invalid_store") from None
            rows.append(row)
        return rows

    def get_datasets(self, *, ids=(), paths=()) -> dict:
        """Return requested committed imports, including ones outside the report sample."""
        return self._invoke("get_datasets", ids=ids, paths=paths)

    def _get_datasets(self, ids, paths) -> dict:
        ids, paths = list(ids), list(paths)
        with self._transaction() as (connection, directory):
            if connection is None:
                _, source_status = self._load_legacy(directory)
                return _success(datasets=[], source_status=source_status)
            datasets = self._datasets(connection, limit=None, ids=ids, paths=paths) if ids or paths else []
            return _success(datasets=datasets, source_status="available")

    def list_credentials(
        self,
        *,
        query: str | None = None,
        validation_status: str | None = None,
        limit: int = 20,
        offset: int = 0,
        exclude_ids=(),
    ) -> dict:
        return self._invoke(
            "list_credentials",
            query=query,
            validation_status=validation_status,
            limit=limit,
            offset=offset,
            exclude_ids=exclude_ids,
        )

    def _list_credentials(self, *, query=None, validation_status=None, limit=20, offset=0, exclude_ids=()):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise CredentialError("invalid_arguments")
        where, values, excluded = self._filters(query, validation_status, exclude_ids)
        with self._transaction() as (connection, directory):
            if connection is None:
                document, source_status = self._load_legacy(directory)
                overall = [row for row in document["credentials"] if row["id"] not in excluded]
                rows = [
                    row
                    for row in overall
                    if (validation_status is None or row["validation_status"] == validation_status)
                    and (
                        not query or query.casefold() in "\n".join(row[field] for field in _LIMITS).casefold()
                    )
                ]
                rows.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
                return _success(
                    credentials=[_current(row) for row in rows[offset : offset + limit]],
                    total=len(rows),
                    overall_total=len(overall),
                    summary=self._legacy_summary(overall),
                    limit=limit,
                    offset=offset,
                    has_more=offset + limit < len(rows),
                    source_status=source_status,
                )
            overall_total, summary = self._summary(connection, exclude_ids=excluded)
            total = connection.execute("SELECT COUNT(*) FROM credentials" + where, values).fetchone()[0]
            rows = [
                _current(self._decode(row[0]))
                for row in connection.execute(
                    "SELECT record FROM credentials"
                    + where
                    + " ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
                    [*values, limit, offset],
                )
            ]
            return _success(
                credentials=rows,
                total=total,
                overall_total=overall_total,
                summary=summary,
                limit=limit,
                offset=offset,
                has_more=offset + limit < total,
                source_status="available",
            )

    def get_credential(self, credential_id: str, *, include_history: bool = False) -> dict:
        return self._invoke("get_credential", credential_id=credential_id, include_history=include_history)

    def _get_credential(self, credential_id: str, include_history: bool) -> dict:
        _id(credential_id)
        if type(include_history) is not bool:
            raise CredentialError("invalid_arguments")
        with self._transaction() as (connection, directory):
            row = (
                self._fetch(connection, credential_id)
                if connection is not None
                else _find(self._load_legacy(directory)[0], credential_id)
            )
            if row is None:
                raise CredentialError("credential_not_found")
            return _success(credential=_current(row, include_history=include_history))

    def get_credentials(self, ids) -> dict:
        return self._invoke("get_credentials", ids=ids)

    def existing_credential_ids(self, ids) -> dict:
        """Resolve references through the ID index without loading credential bodies."""
        return self._invoke("existing_credential_ids", ids=ids)

    def _existing_credential_ids(self, ids) -> dict:
        with self._transaction() as (connection, directory):
            if connection is None:
                document, source_status = self._load_legacy(directory)
                known = {row["id"] for row in document["credentials"]}
                found = [value for value in dict.fromkeys(_id(value) for value in ids) if value in known]
            else:
                source_status = "available"
                found, batch, seen = [], [], set()
                for value in ids:
                    value = _id(value)
                    if value not in seen:
                        seen.add(value)
                        batch.append(value)
                    if len(batch) >= 500:
                        found.extend(self._existing_batch(connection, batch))
                        batch.clear()
                found.extend(self._existing_batch(connection, batch))
            return _success(credential_ids=found, source_status=source_status)

    @staticmethod
    def _existing_batch(connection, batch: list[str]) -> list[str]:
        if not batch:
            return []
        placeholders = ",".join("?" for _ in batch)
        return [
            row[0]
            for row in connection.execute(
                f"SELECT id FROM credentials WHERE id IN ({placeholders})",
                batch,
            )
        ]

    def _get_credentials(self, ids) -> dict:
        with self._transaction() as (connection, directory):
            if connection is None:
                document, source_status = self._load_legacy(directory)
                lookup = {row["id"]: row for row in document["credentials"]}
                rows = [
                    _current(lookup[value])
                    for value in dict.fromkeys(_id(value) for value in ids)
                    if value in lookup
                ]
            else:
                source_status = "available"
                rows, batch, seen = [], [], set()
                for value in ids:
                    value = _id(value)
                    if value not in seen:
                        seen.add(value)
                        batch.append(value)
                    if len(batch) >= 500:
                        rows.extend(self._get_batch(connection, batch))
                        batch.clear()
                rows.extend(self._get_batch(connection, batch))
            return _success(credentials=rows, source_status=source_status)

    def _get_batch(self, connection, batch: list[str]) -> list[dict]:
        if not batch:
            return []
        placeholders = ",".join("?" for _ in batch)
        return [
            _current(self._decode(row[0]))
            for row in connection.execute(
                f"SELECT record FROM credentials WHERE id IN ({placeholders})",
                batch,
            )
        ]

    def snapshot(self, limit: int | None = None, *, exclude_ids=()) -> dict:
        return self._invoke("snapshot", limit=limit, exclude_ids=exclude_ids, prioritized=False)

    def report_snapshot(self, limit: int = 100, *, exclude_ids=()) -> dict:
        return self._invoke("snapshot", limit=limit, exclude_ids=exclude_ids, prioritized=True)

    def _snapshot(self, limit: int | None, exclude_ids, prioritized: bool) -> dict:
        if limit is not None and (type(limit) is not int or limit < 0):
            raise CredentialError("invalid_arguments")
        where, values, excluded = self._filters(exclude_ids=exclude_ids)
        with self._transaction() as (connection, directory):
            if connection is None:
                document, source_status = self._load_legacy(directory)
                rows = [row for row in document["credentials"] if row["id"] not in excluded]
                summary, total = self._legacy_summary(rows), len(rows)
                if prioritized:
                    rows.sort(
                        key=lambda row: (
                            row["validation_status"] == "validated",
                            _SEVERITIES.index(row["severity"]),
                            row["updated_at"],
                            row["id"],
                        ),
                        reverse=True,
                    )
                rows = rows if limit is None else rows[:limit]
                rows = [
                    {key: copy.deepcopy(value) for key, value in row.items() if key in _VERSION_KEYS}
                    for row in rows
                ]
                return _success(
                    credentials=rows,
                    total=total,
                    overall_total=total,
                    summary=summary,
                    datasets=[],
                    dataset_count=0,
                    source_status=source_status,
                    sampled=len(rows) < total,
                )
            total, summary = self._summary(connection, exclude_ids=excluded)
            order = "validation_rank DESC,severity_rank DESC,updated_at DESC,id" if prioritized else "rowid"
            sql = "SELECT record FROM credentials" + where + " ORDER BY " + order
            if limit is not None:
                sql += " LIMIT ?"
                values.append(limit)
            rows = []
            for (raw,) in connection.execute(sql, values):
                row = self._decode(raw)
                rows.append({key: value for key, value in row.items() if key in _VERSION_KEYS})
            return _success(
                credentials=rows,
                total=total,
                overall_total=total,
                summary=summary,
                datasets=self._datasets(connection),
                source_status="available",
                dataset_count=connection.execute("SELECT COUNT(*) FROM credential_datasets").fetchone()[0],
                sampled=len(rows) < total,
            )

    def iter_credentials(self, *, query=None, validation_status=None, exclude_ids=()):
        """Yield all current records from one read snapshot; errors are never empty output."""
        try:
            where, values, excluded = self._filters(query, validation_status, exclude_ids)
            with self._transaction() as (connection, directory):
                if connection is None:
                    document, _ = self._load_legacy(directory)
                    for row in document["credentials"]:
                        if (
                            row["id"] in excluded
                            or (
                                validation_status is not None
                                and row["validation_status"] != validation_status
                            )
                            or (
                                query
                                and query.casefold()
                                not in "\n".join(row[field] for field in _LIMITS).casefold()
                            )
                        ):
                            continue
                        yield _current(row)
                else:
                    for (raw,) in connection.execute(
                        "SELECT record FROM credentials" + where + " ORDER BY updated_at DESC,id",
                        values,
                    ):
                        yield _current(self._decode(raw))
        except DatabaseError as exc:
            raise CredentialError(exc.code) from None
        except OSError:
            raise CredentialError("storage_unavailable") from None

    @staticmethod
    def _history(row: dict, author: dict) -> None:
        if row["revision"] >= 1_000_000_000:
            raise CredentialError("content_limit")
        row["history"].append(
            {key: copy.deepcopy(value) for key, value in row.items() if key in _VERSION_KEYS}
        )
        if len(row["history"]) > MAX_HISTORY:
            del row["history"][:-MAX_HISTORY]
            row["history_truncated"] = True
        row["revision"] += 1
        row["updated_by"] = author
        row["updated_at"] = datetime.now(UTC).isoformat()

    def _new_row(self, fields: dict, author: dict) -> dict:
        password_present = fields["password"] is not None
        if not password_present:
            fields["password"] = ""
        if fields["hash"] and not fields["password"] and fields["secret_type"] == "password":
            fields["secret_type"] = "hash"
        for key, limit in _LIMITS.items():
            _text(fields[key], limit)
        _choice(fields["secret_type"], SECRET_TYPES)
        _choice(fields["severity"], _SEVERITIES)
        _choice(fields["validation_status"], VALIDATION_STATUSES)
        if not password_present and not fields["hash"] and fields["secret_type"] != "username":
            raise CredentialError("invalid_arguments")
        stamp = datetime.now(UTC).isoformat()
        row = {
            **fields,
            "id": _credential_id(fields),
            "revision": 1,
            "created_by": author,
            "updated_by": author,
            "created_at": stamp,
            "updated_at": stamp,
        }
        row["sources"] = [_provenance(row, author, row["source"])]
        try:
            _validate_version(row)
        except CredentialError:
            raise CredentialError("invalid_arguments") from None
        row.update(history=[], history_truncated=False)
        return row

    def _upsert(self, connection, row: dict, author: dict) -> tuple[dict, bool]:
        existing = self._fetch(connection, row["id"])
        if existing is None:
            self._put(connection, row)
            return row, True
        changes = {}
        for key in ("source", "note"):
            value = row[key]
            if value and f"\n{value}\n" not in f"\n{existing[key]}\n":
                changes[key] = _text("\n".join(filter(None, [existing[key], value])), _LIMITS[key])
        provenance = row["sources"][0]
        if provenance not in existing["sources"]:
            if len(existing["sources"]) >= MAX_SOURCES:
                raise CredentialError("content_limit")
            changes["sources"] = [*existing["sources"], provenance]
        if changes:
            self._history(existing, author)
            existing.update(changes)
            self._put(connection, existing)
        return existing, False

    def record_credential(
        self,
        *,
        host: str = "",
        username: str = "",
        password: str | None = None,
        hash: str = "",
        secret_type: str = "password",
        source: str = "",
        severity: str = "",
        note: str = "",
        validation_status: str = "unverified",
        validation_evidence: str = "",
        agent_id: str,
        agent_name: str,
    ) -> dict:
        return self._invoke(
            "record_credential",
            fields={
                "host": host,
                "username": username,
                "password": password,
                "hash": hash,
                "secret_type": secret_type,
                "source": source,
                "severity": severity,
                "note": note,
                "validation_status": validation_status,
                "validation_evidence": validation_evidence,
            },
            agent_id=agent_id,
            agent_name=agent_name,
        )

    def _record_credential(self, fields: dict, agent_id: str, agent_name: str) -> dict:
        author = _author(agent_id, agent_name)
        row = self._new_row(fields, author)
        with self._transaction(write=True) as (connection, _):
            row, created = self._upsert(connection, row, author)
            return _success(credential=_current(row), created=created)

    def update_credential(
        self,
        credential_id: str,
        *,
        expected_revision: int,
        validation_status: str | None = None,
        validation_evidence: str | None = None,
        note: str | None = None,
        source: str | None = None,
        severity: str | None = None,
        agent_id: str,
        agent_name: str,
    ) -> dict:
        return self._invoke(
            "update_credential",
            credential_id=credential_id,
            expected_revision=expected_revision,
            validation_status=validation_status,
            validation_evidence=validation_evidence,
            note=note,
            source=source,
            severity=severity,
            agent_id=agent_id,
            agent_name=agent_name,
        )

    def _update_credential(self, credential_id, expected_revision, agent_id, agent_name, **fields):
        _id(credential_id)
        _revision(expected_revision)
        author = _author(agent_id, agent_name)
        changes = {key: value for key, value in fields.items() if value is not None}
        if not changes:
            raise CredentialError("invalid_arguments")
        for key, value in changes.items():
            if key == "validation_status":
                _choice(value, VALIDATION_STATUSES)
            elif key == "severity":
                _choice(value, _SEVERITIES)
            else:
                _text(value, _LIMITS[key])
        with self._transaction(write=True) as (connection, _):
            row = self._fetch(connection, credential_id)
            if row is None:
                raise CredentialError("credential_not_found")
            if row["revision"] != expected_revision:
                raise CredentialError("revision_conflict", current_revision=row["revision"])
            status = changes.get("validation_status", row["validation_status"])
            evidence = changes.get("validation_evidence", row["validation_evidence"])
            if status in {"validated", "failed"} and (
                not evidence.strip()
                or (status != row["validation_status"] and "validation_evidence" not in changes)
            ):
                raise CredentialError("invalid_arguments")
            if status != row["validation_status"] and "validation_evidence" not in changes:
                changes["validation_evidence"] = ""
            if "source" in changes:
                provenance = _provenance(row, author, changes["source"])
                if provenance not in row["sources"]:
                    if len(row["sources"]) >= MAX_SOURCES:
                        raise CredentialError("content_limit")
                    changes["sources"] = [*row["sources"], provenance]
            if any(row[key] != value for key, value in changes.items()):
                self._history(row, author)
                row.update(changes)
                self._put(connection, row)
            return _success(credential=_current(row))

    def import_rows(
        self,
        rows,
        *,
        source: str,
        agent_id: str,
        agent_name: str,
        dataset_path: str = "",
        dataset_metadata: dict | None = None,
        cancelled=None,
    ) -> dict:
        return self._invoke(
            "import_rows",
            rows=rows,
            source=source,
            agent_id=agent_id,
            agent_name=agent_name,
            dataset_path=dataset_path,
            dataset_metadata=dataset_metadata,
            cancelled=cancelled,
        )

    def _import_rows(self, rows, source, agent_id, agent_name, dataset_path, dataset_metadata, cancelled):
        author = _author(agent_id, agent_name)
        source = _text(source, 4096)
        dataset_path = _text(dataset_path, 4096)
        if cancelled is not None and not callable(cancelled):
            raise CredentialError("invalid_arguments")
        if dataset_metadata is not None and not isinstance(dataset_metadata, dict):
            raise CredentialError("invalid_arguments")
        defaults = {
            "host": "",
            "username": "",
            "password": None,
            "hash": "",
            "secret_type": "password",
            "source": source,
            "severity": "",
            "note": "",
            "validation_status": "unverified",
            "validation_evidence": "",
        }
        rows_read = inserted = 0
        with self._transaction(write=True, cancelled=cancelled) as (connection, _):
            for values in rows:
                if cancelled is not None and cancelled():
                    raise CredentialError("cancelled")
                if not isinstance(values, dict) or set(values) - set(defaults):
                    raise CredentialError("invalid_dataset")
                fields = {**defaults, **values}
                fields["source"] = fields["source"] or source
                try:
                    row = self._new_row(fields, author)
                except CredentialError as exc:
                    if exc.code == "invalid_arguments":
                        raise CredentialError("invalid_dataset") from None
                    raise
                _, created = self._upsert(connection, row, author)
                rows_read += 1
                inserted += int(created)
            if cancelled is not None and cancelled():
                raise CredentialError("cancelled")
            metadata = dataset_metadata or {}
            digest, size = metadata.get("sha256", ""), metadata.get("size", 0)
            if not isinstance(digest, str) or (digest and not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise CredentialError("invalid_dataset")
            if type(size) is not int or size < 0:
                raise CredentialError("invalid_dataset")
            dataset = {
                "id": f"dataset-{uuid.uuid4().hex}",
                "source": source,
                "path": dataset_path,
                "rows_read": rows_read,
                "inserted": inserted,
                "duplicates": rows_read - inserted,
                "created_by": author,
                "created_at": datetime.now(UTC).isoformat(),
                "sha256": digest,
                "size": size,
            }
            connection.execute(
                "INSERT INTO credential_datasets (id,path,sha256,size,created_at,record) "
                "VALUES (?,?,?,?,?,?)",
                (
                    dataset["id"],
                    dataset_path,
                    digest,
                    size,
                    dataset["created_at"],
                    json.dumps(dataset, ensure_ascii=False),
                ),
            )
            total, summary = self._summary(connection)
            return _success(
                rows_read=rows_read,
                inserted=inserted,
                duplicates=rows_read - inserted,
                total=total,
                dataset_id=dataset["id"],
                summary=summary,
            )
