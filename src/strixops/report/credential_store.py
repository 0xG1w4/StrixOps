"""Run-owned credential register with attributed, revisioned, atomic updates.

Agents record exact observations here; the unified inventory generates CSV.
Every mutation reloads under a process-safe file lock, while readers never
create files. Console readers may use the pure parser and projection helpers.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    """Lazy register; independent processes serialize mutations and reread state."""

    def __init__(self, run_dir: Path, lock: Any = None) -> None:
        self.run_dir = Path(run_dir).absolute()
        self._lock = lock if lock is not None else threading.RLock()

    def _directory(self, *, create: bool = False) -> int | None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root = os.open(self.run_dir, flags)
        try:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(".state", mode=0o700, dir_fd=root)
            try:
                return os.open(".state", flags, dir_fd=root)
            except FileNotFoundError:
                return None
        finally:
            os.close(root)

    @staticmethod
    def _stat(directory: int, filename: str = "credentials.json") -> tuple | None:
        try:
            info = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CredentialError("unsafe_storage")
        if info.st_size > MAX_STORE_BYTES:
            raise CredentialError("store_limit")
        return _fingerprint(info)

    def _load(self, directory: int | None) -> tuple[dict, tuple | None]:
        if directory is None:
            return empty_document(), None
        expected = self._stat(directory)
        if expected is None:
            return empty_document(), None
        descriptor = os.open(
            "credentials.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CredentialError("unsafe_storage")
            if info.st_size > MAX_STORE_BYTES:
                raise CredentialError("store_limit")
            raw = stream.read(MAX_STORE_BYTES + 1)
            if _fingerprint(info) != expected or _fingerprint(os.fstat(stream.fileno())) != expected:
                raise CredentialError("storage_unavailable")
        if self._stat(directory) != expected or len(raw) != expected[2]:
            raise CredentialError("storage_unavailable")
        return parse_document(raw), expected

    @contextmanager
    def _transaction(self, *, write: bool = False):
        directory = self._directory(create=write)
        descriptor = None
        try:
            if write:
                if directory is None:
                    raise CredentialError("storage_unavailable")
                self._stat(directory, "credentials.lock")
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                try:
                    descriptor = os.open(
                        "credentials.lock",
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory,
                    )
                except FileExistsError:
                    descriptor = os.open("credentials.lock", flags, dir_fd=directory)
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise CredentialError("unsafe_storage")
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise CredentialError("storage_unavailable") from None
                        time.sleep(0.01)
                if self._stat(directory, "credentials.lock") != _fingerprint(info):
                    raise CredentialError("storage_unavailable")
            document, fingerprint = self._load(directory)
            yield document, directory, fingerprint
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                os.close(directory)

    def _save(self, directory: int, document: dict, expected: tuple | None) -> None:
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_STORE_BYTES:
            raise CredentialError("store_limit")
        temporary = f".credentials-{uuid.uuid4().hex}.tmp"
        try:
            if self._stat(directory) != expected:
                raise CredentialError("storage_unavailable")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if self._stat(directory) != expected:
                raise CredentialError("storage_unavailable")
            os.replace(temporary, "credentials.json", src_dir_fd=directory, dst_dir_fd=directory)
            # Publication succeeded: a directory-fsync failure must not report rollback.
            with suppress(OSError):
                os.fsync(directory)
        finally:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory)

    def _invoke(self, operation: str, **kwargs: Any) -> dict:
        try:
            with self._lock:
                return getattr(self, f"_{operation}")(**kwargs)
        except CredentialError as exc:
            return error_result(exc.code, current_revision=exc.current_revision)
        except OSError:
            return error_result("storage_unavailable")
        except Exception:
            return error_result("internal_error")

    def snapshot(self) -> dict:
        return self._invoke("snapshot")

    def _snapshot(self) -> dict:
        with self._transaction() as (document, _, fingerprint):
            result = project_snapshot(document)
            result["source_status"] = "available" if fingerprint else "missing"
            return result

    def get_credential(self, credential_id: str, *, include_history: bool = False) -> dict:
        return self._invoke("get_credential", credential_id=credential_id, include_history=include_history)

    def _get_credential(self, credential_id: str, include_history: bool) -> dict:
        if type(include_history) is not bool:
            raise CredentialError("invalid_arguments")
        with self._transaction() as (document, _, _):
            return _success(
                credential=_current(_find(document, credential_id), include_history=include_history)
            )

    def list_credentials(
        self,
        *,
        query: str | None = None,
        validation_status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        return self._invoke(
            "list_credentials", query=query, validation_status=validation_status, limit=limit, offset=offset
        )

    def _list_credentials(
        self, query: str | None, validation_status: str | None, limit: int, offset: int
    ) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise CredentialError("invalid_arguments")
        query = _text(query, 500).casefold() if query is not None else ""
        if validation_status is not None:
            _choice(validation_status, VALIDATION_STATUSES)
        with self._transaction() as (document, _, _):
            rows = [
                row
                for row in document["credentials"]
                if (validation_status is None or row["validation_status"] == validation_status)
                and (not query or query in "\n".join(row[field] for field in _LIMITS).casefold())
            ]
            rows.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
            return _success(
                credentials=[_current(row) for row in rows[offset : offset + limit]],
                total=len(rows),
                limit=limit,
                offset=offset,
                has_more=offset + limit < len(rows),
            )

    @staticmethod
    def _history(row: dict, author: dict) -> None:
        if row["revision"] >= 1_000_000_000:
            raise CredentialError("content_limit")
        row["history"].append({key: copy.deepcopy(row[key]) for key in _VERSION_KEYS})
        if len(row["history"]) > MAX_HISTORY:
            del row["history"][:-MAX_HISTORY]
            row["history_truncated"] = True
        row["revision"] += 1
        row["updated_by"] = author
        row["updated_at"] = datetime.now(UTC).isoformat()

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
            host=host,
            username=username,
            password=password,
            hash=hash,
            secret_type=secret_type,
            source=source,
            severity=severity,
            note=note,
            validation_status=validation_status,
            validation_evidence=validation_evidence,
            agent_id=agent_id,
            agent_name=agent_name,
        )

    def _record_credential(self, **fields: Any) -> dict:
        author = _author(fields.pop("agent_id"), fields.pop("agent_name"))
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
        with self._transaction(write=True) as (document, directory, fingerprint):
            existing = next((saved for saved in document["credentials"] if saved["id"] == row["id"]), None)
            if existing is None:
                if len(document["credentials"]) >= MAX_CREDENTIALS:
                    raise CredentialError("credential_limit")
                document["credentials"].append(row)
            else:
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
                else:
                    return _success(credential=_current(existing), created=False)
                row = existing
            self._save(directory, document, fingerprint)
            return _success(credential=_current(row), created=existing is None)

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

    def _update_credential(
        self,
        credential_id: str,
        expected_revision: int,
        agent_id: str,
        agent_name: str,
        **fields: Any,
    ) -> dict:
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
        with self._transaction(write=True) as (document, directory, fingerprint):
            row = _find(document, credential_id)
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
                self._save(directory, document, fingerprint)
            return _success(credential=_current(row))
