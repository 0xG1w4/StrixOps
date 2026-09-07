"""Authoritative project-assignment sidecar tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strixops.console import project_assignment


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    path = tmp_path / "example-com_ab12"
    path.mkdir()
    return path


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_assignment_roundtrip_is_explicit_and_private(run_dir: Path):
    written = project_assignment.write_assignment(run_dir, "prj_0123abcd", source="launch")
    loaded = project_assignment.read_assignment(run_dir)

    assert written.assigned is True
    assert loaded == written
    assert loaded.storage == "sidecar"
    assert loaded.explicit is True
    assert (project_assignment.assignment_path(run_dir).stat().st_mode & 0o777) == 0o600
    assert list(run_dir.glob("..project_assignment.json.*.tmp")) == []


def test_clear_writes_tombstone_and_does_not_resurrect_legacy_value(run_dir: Path):
    _write_json(run_dir / "run.json", {"scan_config": {"project_id": "prj_stale"}})
    _write_json(run_dir / ".console_launch.json", {"project_id": "prj_older"})

    cleared = project_assignment.clear_assignment(run_dir, source="manual")
    loaded = project_assignment.read_assignment(run_dir, legacy_fallback=True)
    payload = json.loads(project_assignment.assignment_path(run_dir).read_text(encoding="utf-8"))

    assert cleared.assigned is False
    assert loaded.project_id == ""
    assert loaded.storage == "sidecar"
    assert loaded.explicit is True
    assert payload["state"] == "unassigned"
    assert payload["project_id"] == ""


def test_legacy_fallback_prefers_run_record(run_dir: Path):
    _write_json(run_dir / "run.json", {"scan_config": {"project_id": "prj_manual"}})
    _write_json(run_dir / ".console_launch.json", {"project_id": "prj_launch"})

    assignment = project_assignment.read_assignment(run_dir)
    assert assignment.project_id == "prj_manual"
    assert assignment.storage == "legacy-run"
    assert assignment.explicit is False


def test_legacy_launch_recovers_assignment_dropped_from_run_record(run_dir: Path):
    _write_json(run_dir / "run.json", {"scan_config": {"target": "example.com"}})
    _write_json(run_dir / ".console_launch.json", {"project_id": "prj_launch"})

    assignment = project_assignment.read_assignment(run_dir)
    assert assignment.project_id == "prj_launch"
    assert assignment.storage == "legacy-launch"


def test_corrupt_sidecar_fails_closed_without_legacy_fallback(run_dir: Path):
    _write_json(run_dir / "run.json", {"scan_config": {"project_id": "prj_legacy"}})
    project_assignment.assignment_path(run_dir).write_text("{broken", encoding="utf-8")

    with pytest.raises(project_assignment.AssignmentStoreError, match="cannot read"):
        project_assignment.read_assignment(run_dir, legacy_fallback=True)


def test_explicit_migration_snapshots_legacy_assignment(run_dir: Path):
    _write_json(run_dir / "run.json", {"scan_config": {"project_id": "prj_legacy"}})

    migrated = project_assignment.migrate_legacy_assignment(run_dir)
    _write_json(run_dir / "run.json", {"scan_config": {"project_id": "prj_changed"}})

    assert migrated.project_id == "prj_legacy"
    assert migrated.source == "migration"
    assert project_assignment.project_id_for_run(run_dir) == "prj_legacy"


def test_migration_without_legacy_data_creates_tombstone(run_dir: Path):
    migrated = project_assignment.migrate_legacy_assignment(run_dir)
    _write_json(run_dir / ".console_launch.json", {"project_id": "prj_late"})

    assert migrated.assigned is False
    assert migrated.explicit is True
    assert project_assignment.project_id_for_run(run_dir) == ""


@pytest.mark.parametrize(
    "project_id",
    ["", " has-space", "prj/escape", "../escape", "x" * 129, 123],
)
def test_invalid_project_ids_are_rejected(run_dir: Path, project_id: object):
    with pytest.raises(project_assignment.AssignmentStoreError, match="project_id"):
        project_assignment.write_assignment(run_dir, project_id)  # type: ignore[arg-type]


def test_write_refuses_to_create_a_mistyped_run_directory(tmp_path: Path):
    missing = tmp_path / "not-a-run"
    with pytest.raises(project_assignment.AssignmentStoreError, match="does not exist"):
        project_assignment.write_assignment(missing, "prj_0123abcd")


def test_invalid_sidecar_state_is_rejected(run_dir: Path):
    _write_json(
        project_assignment.assignment_path(run_dir),
        {
            "schema_version": 1,
            "state": "unassigned",
            "project_id": "prj_impossible",
            "updated_at": "2026-09-05T00:00:00Z",
            "source": "manual",
        },
    )
    with pytest.raises(project_assignment.AssignmentStoreError, match="empty project_id"):
        project_assignment.read_assignment(run_dir)
