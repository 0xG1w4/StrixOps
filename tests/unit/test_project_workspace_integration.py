"""Project workspace integration: scope migration, membership, launch guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from strixops.console import project_assignment, projects_store, server


@pytest.fixture()
def workspace_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    projects_file = tmp_path / "projects.json"
    monkeypatch.setenv("STRIXOPS_PROJECTS_FILE", str(projects_file))
    monkeypatch.setattr(server, "state", server.ConsoleState(tmp_path / "runs"))
    server.state.runs_root.mkdir()
    return projects_file


def _save_project(*, scope_rules: list[dict] | None = None) -> dict:
    payload: dict = {"name": "Customer portal", "description": "", "color": "cyan"}
    if scope_rules is not None:
        payload["scope_rules"] = scope_rules
    project, errors = projects_store.sanitize_project(payload)
    assert errors == []
    projects_store.save_projects({"schema_version": 2, "projects": [project]})
    return project


def test_v1_project_receives_explicit_unrestricted_scope(workspace_store: Path) -> None:
    workspace_store.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": [
                    {
                        "id": "prj_legacy",
                        "name": "Legacy",
                        "description": "",
                        "color": "gold",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    project = projects_store.load_projects()["projects"][0]

    assert project["scope"]["mode"] == "unrestricted"
    assert project["scope"]["entries"] == [{"kind": "any", "value": "*"}]
    assert project["scope_revision"] == 1


def test_existing_project_description_can_be_cleared(workspace_store: Path) -> None:
    existing, errors = projects_store.sanitize_project(
        {"name": "Customer portal", "description": "Legacy description"}
    )
    assert errors == []

    updated, errors = projects_store.sanitize_project(
        {"description": ""},
        existing=existing,
    )

    assert errors == []
    assert updated["description"] == ""


def test_assignment_sidecar_survives_engine_run_record_rewrite(workspace_store: Path) -> None:
    project = _save_project()
    run_dir = server.state.runs_root / "portal_abcd"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"status": "running", "scan_config": {"target": "portal.example.com"}}),
        encoding="utf-8",
    )
    project_assignment.write_assignment(run_dir, project["id"], source="test")

    # This is the engine's old destructive behavior: its next save contains
    # no console-only project_id.  The sidecar remains authoritative.
    (run_dir / "run.json").write_text(
        json.dumps({"status": "completed", "scan_config": {"target": "portal.example.com"}}),
        encoding="utf-8",
    )

    assert projects_store.project_runs(project["id"]) == [run_dir]
    assert server._build_summary(run_dir)["project_id"] == project["id"]


def test_launch_project_scope_is_enforced_before_spawn(workspace_store: Path) -> None:
    project = _save_project(
        scope_rules=[
            {
                "kind": "domain",
                "value": "example.com",
                "include_subdomains": True,
            }
        ]
    )

    allowed = server.ScanBody(
        target="https://api.example.com/login",
        scan_type="web",
        project_id=project["id"],
        dry_run=True,
    )
    assert server._validated_launch_project(allowed)["id"] == project["id"]

    blocked = server.ScanBody(
        target="https://outside.example.net",
        scan_type="web",
        project_id=project["id"],
        dry_run=True,
    )
    with pytest.raises(HTTPException) as caught:
        server._validated_launch_project(blocked)
    assert caught.value.status_code == 422
    assert "outside the project scope" in str(caught.value.detail)
