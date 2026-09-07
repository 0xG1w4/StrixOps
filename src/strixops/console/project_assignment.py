"""Authoritative per-run project assignment sidecar.

``run.json`` is owned and rewritten by the engine, so console-only metadata
must not be stored there as its source of truth.  This module persists project
membership in ``.project_assignment.json`` via an atomic sibling rename.

Clearing an assignment writes an explicit ``unassigned`` tombstone instead of
deleting the file.  The tombstone is important: it prevents stale legacy
``run.json`` or ``.console_launch.json`` values from resurrecting an assignment
the operator deliberately cleared.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

ASSIGNMENT_FILE = ".project_assignment.json"
LEGACY_LAUNCH_FILE = ".console_launch.json"
SCHEMA_VERSION = 1
MAX_SIDECAR_BYTES = 64 * 1024

STATE_ASSIGNED = "assigned"
STATE_UNASSIGNED = "unassigned"

_PROJECT_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,127})\Z")
_SOURCE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,63})\Z")
_SIDECAR_KEYS = {"schema_version", "state", "project_id", "updated_at", "source"}

AssignmentStorage = Literal["sidecar", "legacy-run", "legacy-launch", "none"]


class AssignmentStoreError(RuntimeError):
    """The assignment is malformed or cannot be persisted safely."""


@dataclass(frozen=True, slots=True)
class ProjectAssignment:
    """Resolved project membership and where it came from."""

    project_id: str
    storage: AssignmentStorage
    updated_at: str = ""
    source: str = ""
    explicit: bool = False

    @property
    def assigned(self) -> bool:
        return bool(self.project_id)


def assignment_path(run_dir: Path) -> Path:
    return Path(run_dir) / ASSIGNMENT_FILE


def write_assignment(
    run_dir: Path,
    project_id: str,
    *,
    source: str = "console",
) -> ProjectAssignment:
    """Atomically assign an existing run directory to a project.

    An empty project id is rejected so omitted form fields cannot accidentally
    clear membership.  Use :func:`clear_assignment` for that explicit action.
    Project existence is intentionally checked by the project store/API layer.
    """
    run_path = _validated_run_dir(run_dir)
    normalized_id = _validate_project_id(project_id)
    normalized_source = _validate_source(source)
    updated_at = _utc_now()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "state": STATE_ASSIGNED,
        "project_id": normalized_id,
        "updated_at": updated_at,
        "source": normalized_source,
    }
    _atomic_write_json(run_path / ASSIGNMENT_FILE, payload)
    return ProjectAssignment(
        project_id=normalized_id,
        storage="sidecar",
        updated_at=updated_at,
        source=normalized_source,
        explicit=True,
    )


def clear_assignment(run_dir: Path, *, source: str = "console") -> ProjectAssignment:
    """Atomically persist an explicit unassigned tombstone for a run."""
    run_path = _validated_run_dir(run_dir)
    normalized_source = _validate_source(source)
    updated_at = _utc_now()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "state": STATE_UNASSIGNED,
        "project_id": "",
        "updated_at": updated_at,
        "source": normalized_source,
    }
    _atomic_write_json(run_path / ASSIGNMENT_FILE, payload)
    return ProjectAssignment(
        project_id="",
        storage="sidecar",
        updated_at=updated_at,
        source=normalized_source,
        explicit=True,
    )


def read_assignment(run_dir: Path, *, legacy_fallback: bool = True) -> ProjectAssignment:
    """Read authoritative membership, optionally consulting legacy metadata.

    A present but malformed sidecar raises and never falls back.  Falling back
    in that situation could bypass an explicit clear or attach a run to the
    wrong project.
    """
    run_path = Path(run_dir)
    path = run_path / ASSIGNMENT_FILE
    if path.exists() or path.is_symlink():
        return _read_sidecar(path)
    if legacy_fallback:
        return read_legacy_assignment(run_path)
    return ProjectAssignment(project_id="", storage="none")


def project_id_for_run(run_dir: Path, *, legacy_fallback: bool = True) -> str:
    """Convenience accessor for project aggregation/filtering code."""
    return read_assignment(run_dir, legacy_fallback=legacy_fallback).project_id


def read_legacy_assignment(run_dir: Path) -> ProjectAssignment:
    """Resolve pre-sidecar metadata without mutating it.

    ``run.json`` wins because the old manual assignment endpoint updated that
    file.  The console launch sidecar is a recovery fallback for runs whose
    engine rewrite dropped ``scan_config.project_id``.
    """
    run_path = Path(run_dir)
    record = _read_legacy_json(run_path / "run.json")
    scan_config = record.get("scan_config")
    if isinstance(scan_config, dict):
        project_id = scan_config.get("project_id")
        if project_id not in (None, ""):
            return ProjectAssignment(
                project_id=_validate_project_id(project_id),
                storage="legacy-run",
            )

    launch = _read_legacy_json(run_path / LEGACY_LAUNCH_FILE)
    project_id = launch.get("project_id")
    if project_id not in (None, ""):
        return ProjectAssignment(
            project_id=_validate_project_id(project_id),
            storage="legacy-launch",
        )
    return ProjectAssignment(project_id="", storage="none")


def migrate_legacy_assignment(run_dir: Path) -> ProjectAssignment:
    """Snapshot legacy membership into the authoritative sidecar.

    Runs with no legacy membership receive an explicit tombstone, preventing a
    stale legacy value written later from changing the migration result.
    """
    current = read_assignment(run_dir, legacy_fallback=False)
    if current.storage == "sidecar":
        return current
    legacy = read_legacy_assignment(run_dir)
    if legacy.assigned:
        return write_assignment(run_dir, legacy.project_id, source="migration")
    return clear_assignment(run_dir, source="migration")


def _read_sidecar(path: Path) -> ProjectAssignment:
    if path.is_symlink():
        raise AssignmentStoreError(f"assignment sidecar must not be a symlink: {path}")
    try:
        if path.stat().st_size > MAX_SIDECAR_BYTES:
            raise AssignmentStoreError(f"assignment sidecar is too large: {path}")
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except AssignmentStoreError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssignmentStoreError(f"cannot read assignment sidecar {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AssignmentStoreError("assignment sidecar must contain a JSON object")
    unknown = set(data) - _SIDECAR_KEYS
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise AssignmentStoreError(f"assignment sidecar has unknown fields: {fields}")
    if type(data.get("schema_version")) is not int or data["schema_version"] != SCHEMA_VERSION:
        raise AssignmentStoreError(f"unsupported assignment schema_version (expected {SCHEMA_VERSION})")

    state = data.get("state")
    project_id = data.get("project_id")
    updated_at = data.get("updated_at")
    source = data.get("source")
    if state not in {STATE_ASSIGNED, STATE_UNASSIGNED}:
        raise AssignmentStoreError("assignment state must be 'assigned' or 'unassigned'")
    if not isinstance(project_id, str):
        raise AssignmentStoreError("assignment project_id must be a string")
    if not isinstance(updated_at, str) or not updated_at:
        raise AssignmentStoreError("assignment updated_at must be a non-empty string")
    _validate_timestamp(updated_at)
    normalized_source = _validate_source(source)

    if state == STATE_ASSIGNED:
        normalized_id = _validate_project_id(project_id)
    else:
        if project_id:
            raise AssignmentStoreError("unassigned sidecar must have an empty project_id")
        normalized_id = ""
    return ProjectAssignment(
        project_id=normalized_id,
        storage="sidecar",
        updated_at=updated_at,
        source=normalized_source,
        explicit=True,
    )


def _read_legacy_json(path: Path) -> dict[str, Any]:
    if not path.exists() or path.is_symlink():
        return {}
    try:
        if path.stat().st_size > MAX_SIDECAR_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _validated_run_dir(run_dir: Path) -> Path:
    path = Path(run_dir)
    if not path.exists():
        raise AssignmentStoreError(f"run directory does not exist: {path}")
    if not path.is_dir():
        raise AssignmentStoreError(f"run directory is not a directory: {path}")
    return path


def _validate_project_id(project_id: object) -> str:
    if not isinstance(project_id, str):
        raise AssignmentStoreError("project_id must be a string")
    if project_id != project_id.strip() or not _PROJECT_ID.fullmatch(project_id):
        raise AssignmentStoreError(
            "project_id must be 1-128 characters using letters, numbers, '.', '_', ':', or '-'"
        )
    return project_id


def _validate_source(source: object) -> str:
    if not isinstance(source, str):
        raise AssignmentStoreError("assignment source must be a string")
    if source != source.strip() or not _SOURCE.fullmatch(source):
        raise AssignmentStoreError(
            "assignment source must be 1-64 characters using letters, numbers, '.', '_', ':', or '-'"
        )
    return source


def _validate_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AssignmentStoreError("assignment updated_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AssignmentStoreError("assignment updated_at must include a timezone")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            os.chmod(tmp_path, 0o600)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        _fsync_directory(path.parent)
    except OSError as exc:
        raise AssignmentStoreError(f"cannot persist assignment sidecar {path}: {exc}") from exc
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
