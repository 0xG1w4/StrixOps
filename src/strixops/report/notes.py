"""Bounded, attributed working notes owned by one run, separate from findings.

The engine shares one ``NotesStore`` between its agents. Console readers use
the pure document parser/projections with their own anchored file handling.
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import threading
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CATEGORIES = ("general", "findings", "methodology", "questions", "plan", "wiki")
MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_NOTES = 500
MAX_TITLE_CHARS = 200
MAX_CONTENT_CHARS = 32768
MAX_TAGS = 20
MAX_TAG_CHARS = 64
MAX_HISTORY = 20
PREVIEW_CHARS = 280
_NOTE_ID = re.compile(r"note-[0-9a-f]{32}\Z")
_MESSAGES = {
    "invalid_arguments": "Invalid note arguments; check the tool fields and limits.",
    "invalid_category": "Category must be general, findings, methodology, questions, plan or wiki.",
    "note_not_found": "The note was not found in this run.",
    "note_deleted": "The note is deleted and cannot be changed.",
    "revision_conflict": "The note changed; read its current revision before replacing or deleting it.",
    "note_limit": "This run has reached its note limit, including deleted notes.",
    "content_limit": "The note exceeds a title, content or tag limit.",
    "store_limit": "The notes document exceeds its storage limit.",
    "storage_unavailable": "Notes storage is unavailable or changed outside this run's note store.",
    "invalid_store": "The saved notes document is invalid; it has been preserved unchanged.",
    "unsafe_storage": "Notes storage is not a regular run-owned file or directory.",
    "internal_error": "Notes could not be processed; continue the task using the available evidence.",
}
_VERSION_KEYS = {
    "note_id",
    "title",
    "content",
    "category",
    "tags",
    "revision",
    "created_by",
    "updated_by",
    "created_at",
    "updated_at",
    "deleted",
    "deleted_at",
    "deleted_by",
}


class NotesError(ValueError):
    def __init__(self, code: str, *, current_revision: int | None = None) -> None:
        self.code = code if code in _MESSAGES else "internal_error"
        self.current_revision = current_revision
        super().__init__(_MESSAGES[self.code])


def error_result(code: str, *, current_revision: int | None = None) -> dict:
    error = NotesError(code, current_revision=current_revision)
    result = {"success": False, "continue_task": True, "error_code": error.code, "error": str(error)}
    if type(current_revision) is int and current_revision > 0:
        result["current_revision"] = current_revision
    return result


def empty_document() -> dict:
    return {"schema_version": 1, "notes": []}


def _text(value: Any, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or "\x00" in value:
        raise NotesError("invalid_arguments")
    if len(value) > limit:
        raise NotesError("content_limit")
    # Reject lone surrogates rather than failing later in UTF-8 persistence.
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise NotesError("invalid_arguments") from None
    return value


def _category(value: Any) -> str:
    if not isinstance(value, str) or value not in CATEGORIES:
        raise NotesError("invalid_category")
    return value


def _tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise NotesError("invalid_arguments")
    if len(value) > MAX_TAGS:
        raise NotesError("content_limit")
    result: list[str] = []
    seen: set[str] = set()
    for tag in value:
        tag = _text(tag, MAX_TAG_CHARS).strip()
        if tag.casefold() not in seen:
            result.append(tag)
            seen.add(tag.casefold())
    return result


def _author(agent_id: Any, agent_name: Any) -> dict:
    return {"agent_id": _text(agent_id, 128), "agent_name": _text(agent_name, 200)}


def _revision(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000_000:
        raise NotesError("invalid_arguments")
    return value


def _id(value: Any) -> str:
    if not isinstance(value, str) or not _NOTE_ID.fullmatch(value):
        raise NotesError("invalid_arguments")
    return value


def _timestamp(value: Any) -> None:
    _text(value, 64)
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise NotesError("invalid_store")


def _validate_version(note: Any) -> None:
    if not isinstance(note, dict) or set(note) != _VERSION_KEYS:
        raise NotesError("invalid_store")
    _id(note["note_id"])
    _text(note["title"], MAX_TITLE_CHARS)
    _text(note["content"], MAX_CONTENT_CHARS, empty=True)
    _category(note["category"])
    if _tags(note["tags"]) != note["tags"]:
        raise NotesError("invalid_store")
    _revision(note["revision"])
    for name in ("created_by", "updated_by"):
        value = note[name]
        if not isinstance(value, dict) or set(value) != {"agent_id", "agent_name"}:
            raise NotesError("invalid_store")
        _author(**value)
    _timestamp(note["created_at"])
    _timestamp(note["updated_at"])
    if type(note["deleted"]) is not bool:
        raise NotesError("invalid_store")
    if note["deleted"]:
        _timestamp(note["deleted_at"])
        if note["deleted_by"] != note["updated_by"]:
            raise NotesError("invalid_store")
    elif note["deleted_at"] is not None or note["deleted_by"] is not None:
        raise NotesError("invalid_store")


def parse_document(raw: bytes) -> dict:
    """Parse untrusted stored bytes without exposing content in error messages."""
    if not isinstance(raw, bytes):
        raise NotesError("invalid_store")
    if len(raw) > MAX_STORE_BYTES:
        raise NotesError("store_limit")
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or set(data) != {"schema_version", "notes"}:
            raise NotesError("invalid_store")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise NotesError("invalid_store")
        notes = data["notes"]
        if not isinstance(notes, list) or len(notes) > MAX_NOTES:
            raise NotesError("invalid_store")
        seen: set[str] = set()
        for note in notes:
            if not isinstance(note, dict) or set(note) != _VERSION_KEYS | {"history", "history_truncated"}:
                raise NotesError("invalid_store")
            current = {key: note[key] for key in _VERSION_KEYS}
            _validate_version(current)
            if note["note_id"] in seen:
                raise NotesError("invalid_store")
            seen.add(note["note_id"])
            history = note["history"]
            if not isinstance(history, list) or len(history) > MAX_HISTORY:
                raise NotesError("invalid_store")
            if type(note["history_truncated"]) is not bool:
                raise NotesError("invalid_store")
            expected_start = note["revision"] - len(history)
            if expected_start < 1 or note["history_truncated"] != (expected_start > 1):
                raise NotesError("invalid_store")
            for revision, prior in enumerate(history, expected_start):
                _validate_version(prior)
                if (
                    prior["note_id"] != note["note_id"]
                    or prior["revision"] != revision
                    or prior["created_by"] != note["created_by"]
                    or prior["created_at"] != note["created_at"]
                    or prior["deleted"]
                ):
                    raise NotesError("invalid_store")
        return data
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, OverflowError):
        raise NotesError("invalid_store") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise NotesError("invalid_store")
        result[key] = value
    return result


def _success(**fields: Any) -> dict:
    return {"success": True, "continue_task": True, **fields}


def _find(document: dict, note_id: str) -> dict:
    _id(note_id)
    for note in document["notes"]:
        if note["note_id"] == note_id:
            return note
    raise NotesError("note_not_found")


def project_get(document: dict, note_id: str, *, include_history: bool = False) -> dict:
    if type(include_history) is not bool:
        raise NotesError("invalid_arguments")
    note = copy.deepcopy(_find(document, note_id))
    if not include_history:
        note.pop("history")
    return _success(note=note)


def project_list(
    document: dict,
    *,
    query: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    author: str | None = None,
    include_deleted: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise NotesError("invalid_arguments")
    if type(include_deleted) is not bool:
        raise NotesError("invalid_arguments")
    query = _text(query, 500, empty=True).casefold() if query is not None else ""
    author = _text(author, 200, empty=True).casefold() if author is not None else ""
    if category is not None:
        _category(category)
    required_tags = {value.casefold() for value in _tags(tags if tags is not None else [])}
    matches = []
    for note in document["notes"]:
        if note["deleted"] and not include_deleted:
            continue
        if category is not None and note["category"] != category:
            continue
        if required_tags - {value.casefold() for value in note["tags"]}:
            continue
        if query and query not in "\n".join([note["title"], note["content"], *note["tags"]]).casefold():
            continue
        if author and not any(
            author in value.casefold()
            for field in ("created_by", "updated_by")
            for value in note[field].values()
        ):
            continue
        matches.append(note)
    matches.sort(key=lambda note: (note["updated_at"], note["note_id"]), reverse=True)
    rows = []
    for note in matches[offset : offset + limit]:
        row = {key: copy.deepcopy(value) for key, value in note.items() if key not in {"content", "history"}}
        row.update(preview=note["content"][:PREVIEW_CHARS], content_length=len(note["content"]))
        rows.append(row)
    return _success(
        notes=rows, total=len(matches), limit=limit, offset=offset, has_more=offset + limit < len(matches)
    )


class NotesStore:
    """Lazy run-owned store; damaged storage is never replaced with an empty copy."""

    def __init__(self, run_dir: Path, lock: Any = None) -> None:
        self.run_dir = Path(run_dir).absolute()
        self._lock = lock if lock is not None else threading.RLock()
        self._document: dict | None = None
        self._load_error: str | None = None
        self._fingerprint: tuple | None = None

    def _directory(self, *, create: bool = False) -> int | None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(self.run_dir, flags)
        try:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(".state", mode=0o700, dir_fd=root_fd)
            try:
                return os.open(".state", flags, dir_fd=root_fd)
            except FileNotFoundError:
                return None
        finally:
            os.close(root_fd)

    @staticmethod
    def _stat(directory: int) -> tuple | None:
        try:
            info = os.stat("notes.json", dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise NotesError("unsafe_storage")
        if info.st_size > MAX_STORE_BYTES:
            raise NotesError("store_limit")
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _load(self) -> dict:
        if self._load_error:
            raise NotesError(self._load_error)
        if self._document is not None:
            return self._document
        directory = None
        try:
            directory = self._directory()
            if directory is None or self._stat(directory) is None:
                self._document = empty_document()
            else:
                descriptor = os.open(
                    "notes.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise NotesError("unsafe_storage")
                    raw = stream.read(MAX_STORE_BYTES + 1)
                    fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if self._stat(directory) != fingerprint:
                    raise NotesError("storage_unavailable")
                self._document = parse_document(raw)
                self._fingerprint = fingerprint
            return self._document
        except NotesError as exc:
            self._load_error = exc.code
            raise
        except OSError:
            self._load_error = "storage_unavailable"
            raise NotesError(self._load_error) from None
        finally:
            if directory is not None:
                os.close(directory)

    def _save(self, document: dict) -> None:
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_STORE_BYTES:
            raise NotesError("store_limit")
        directory = self._directory(create=True)
        if directory is None:
            raise NotesError("storage_unavailable")
        temporary = f".notes-{uuid.uuid4().hex}.tmp"
        try:
            if self._stat(directory) != self._fingerprint:
                raise NotesError("storage_unavailable")
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                written = os.fstat(stream.fileno())
            # Check again after writing; do not replace an externally changed file.
            if self._stat(directory) != self._fingerprint:
                raise NotesError("storage_unavailable")
            os.replace(temporary, "notes.json", src_dir_fd=directory, dst_dir_fd=directory)
            # Atomic publication succeeded. Commit memory before fallible diagnostics.
            self._document = document
            with suppress(OSError):
                os.fsync(directory)
            try:
                self._fingerprint = self._published_fingerprint(directory, written, raw)
            except (NotesError, OSError):
                # Publication already succeeded. Never report rollback after commit;
                # an external replacement or a failed stat blocks later mutation.
                self._load_error = "storage_unavailable"
        finally:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
            with suppress(OSError):
                os.close(directory)

    def _published_fingerprint(self, directory: int, written: os.stat_result, raw: bytes) -> tuple:
        """Never adopt a different writer's file as the cached document's identity."""
        descriptor = os.open("notes.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino, info.st_size) != (written.st_dev, written.st_ino, len(raw))
            ):
                raise NotesError("storage_unavailable")
            if stream.read(len(raw) + 1) != raw:
                raise NotesError("storage_unavailable")
            fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if self._stat(directory) != fingerprint:
            raise NotesError("storage_unavailable")
        return fingerprint

    def _invoke(self, operation: str, **kwargs: Any) -> dict:
        try:
            with self._lock:
                return getattr(self, f"_{operation}")(**kwargs)
        except NotesError as exc:
            return error_result(exc.code, current_revision=exc.current_revision)
        except OSError:
            return error_result("storage_unavailable")
        except Exception:
            return error_result("internal_error")

    def list_notes(self, **kwargs: Any) -> dict:
        return self._invoke("list_notes", **kwargs)

    def _list_notes(self, **kwargs: Any) -> dict:
        return project_list(self._load(), **kwargs)

    def get_note(self, note_id: str, *, include_history: bool = False) -> dict:
        return self._invoke("get_note", note_id=note_id, include_history=include_history)

    def _get_note(self, **kwargs: Any) -> dict:
        return project_get(self._load(), **kwargs)

    def create_note(
        self,
        *,
        title: str,
        content: str,
        category: str = "general",
        tags: list[str] | None = None,
        agent_id: str,
        agent_name: str,
    ) -> dict:
        return self._invoke(
            "create_note",
            title=title,
            content=content,
            category=category,
            tags=tags,
            agent_id=agent_id,
            agent_name=agent_name,
        )

    def _create_note(
        self,
        *,
        title: str,
        content: str,
        category: str,
        tags: list[str] | None,
        agent_id: str,
        agent_name: str,
    ) -> dict:
        author = _author(agent_id, agent_name)
        stamp = datetime.now(UTC).isoformat()
        note = {
            "note_id": f"note-{uuid.uuid4().hex}",
            "title": _text(title, MAX_TITLE_CHARS).strip(),
            "content": _text(content, MAX_CONTENT_CHARS, empty=True),
            "category": _category(category),
            "tags": _tags(tags if tags is not None else []),
            "revision": 1,
            "created_by": author,
            "updated_by": author,
            "created_at": stamp,
            "updated_at": stamp,
            "deleted": False,
            "deleted_at": None,
            "deleted_by": None,
            "history": [],
            "history_truncated": False,
        }
        document = copy.deepcopy(self._load())
        if len(document["notes"]) >= MAX_NOTES:
            raise NotesError("note_limit")
        document["notes"].append(note)
        self._save(document)
        return project_get(document, note["note_id"])

    def update_note(
        self,
        note_id: str,
        *,
        agent_id: str,
        agent_name: str,
        expected_revision: int | None = None,
        title: str | None = None,
        content: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        append_content: str | None = None,
    ) -> dict:
        return self._invoke(
            "update_note",
            note_id=note_id,
            agent_id=agent_id,
            agent_name=agent_name,
            expected_revision=expected_revision,
            title=title,
            content=content,
            category=category,
            tags=tags,
            append_content=append_content,
        )

    def _update_note(
        self,
        note_id: str,
        *,
        agent_id: str,
        agent_name: str,
        expected_revision: int | None,
        title: str | None,
        content: str | None,
        category: str | None,
        tags: list[str] | None,
        append_content: str | None,
    ) -> dict:
        replacement = any(value is not None for value in (title, content, category, tags))
        if (append_content is not None and replacement) or (append_content is None and not replacement):
            raise NotesError("invalid_arguments")
        if replacement and expected_revision is None:
            raise NotesError("invalid_arguments")
        document = copy.deepcopy(self._load())
        note = self._editable(document, note_id, expected_revision)
        self._history(note)
        if title is not None:
            note["title"] = _text(title, MAX_TITLE_CHARS).strip()
        if content is not None:
            note["content"] = _text(content, MAX_CONTENT_CHARS, empty=True)
        if category is not None:
            note["category"] = _category(category)
        if tags is not None:
            note["tags"] = _tags(tags)
        if append_content is not None:
            appended = _text(append_content, MAX_CONTENT_CHARS)
            note["content"] = _text(note["content"] + appended, MAX_CONTENT_CHARS, empty=True)
        note["revision"] += 1
        note["updated_by"] = _author(agent_id, agent_name)
        note["updated_at"] = datetime.now(UTC).isoformat()
        self._save(document)
        return project_get(document, note_id)

    def delete_note(self, note_id: str, *, expected_revision: int, agent_id: str, agent_name: str) -> dict:
        return self._invoke(
            "delete_note",
            note_id=note_id,
            expected_revision=expected_revision,
            agent_id=agent_id,
            agent_name=agent_name,
        )

    def _delete_note(self, note_id: str, *, expected_revision: int, agent_id: str, agent_name: str) -> dict:
        _revision(expected_revision)
        document = copy.deepcopy(self._load())
        note = self._editable(document, note_id, expected_revision)
        self._history(note)
        note["revision"] += 1
        note["updated_by"] = note["deleted_by"] = _author(agent_id, agent_name)
        note["updated_at"] = note["deleted_at"] = datetime.now(UTC).isoformat()
        note["deleted"] = True
        self._save(document)
        return project_get(document, note_id)

    @staticmethod
    def _editable(document: dict, note_id: str, expected_revision: int | None) -> dict:
        note = _find(document, note_id)
        if expected_revision is not None and _revision(expected_revision) != note["revision"]:
            raise NotesError("revision_conflict", current_revision=note["revision"])
        if note["deleted"]:
            raise NotesError("note_deleted")
        if note["revision"] >= 1_000_000_000:
            raise NotesError("content_limit")
        return note

    @staticmethod
    def _history(note: dict) -> None:
        note["history"].append({key: copy.deepcopy(note[key]) for key in _VERSION_KEYS})
        if len(note["history"]) > MAX_HISTORY:
            del note["history"][:-MAX_HISTORY]
            note["history_truncated"] = True
