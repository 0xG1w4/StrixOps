"""Shared skill accounting and read-only project scope preflight contracts."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from strixops.console import project_assignment, project_scope, projects_store, server


@pytest.fixture()
def project_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("STRIXOPS_PROJECTS_FILE", str(tmp_path / "projects.json"))
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(server, "state", server.ConsoleState(runs_root))
    with TestClient(server.app) as client:
        yield client


def _save_project(scope: dict | None = None, *, revision: int = 1) -> dict:
    project, errors = projects_store.sanitize_project({"name": "Portal"})
    assert not errors
    project["scope"] = scope if scope is not None else project_scope.default_scope()
    project["scope_revision"] = revision
    projects_store.save_projects({"schema_version": 2, "projects": [project]})
    return project


def _domain_scope() -> dict:
    return {
        "schema_version": 1,
        "mode": "restricted",
        "entries": [
            {"kind": "domain", "value": "example.com", "include_subdomains": True},
            {"kind": "cidr", "value": "10.20.0.0/16"},
        ],
    }


def test_global_and_project_analytics_count_each_injection_once(project_api: TestClient) -> None:
    project = _save_project()
    run_dir = server.state.runs_root / "portal_1234"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"status": "completed", "scan_config": {"target": "example.com"}}),
        encoding="utf-8",
    )
    events = []
    for index in range(13):
        events.extend(
            [
                {
                    "event_type": "tool.execution.started",
                    "payload": {
                        "args": {
                            "name": f"child-{index}",
                            "task": "Inspect the target",
                            "skills": ["vulnerabilities/xss"],
                        }
                    },
                },
                {
                    "event_type": "agent.created",
                    "actor": {"agent_id": f"child-{index}"},
                    "payload": {"skills": ["vulnerabilities/xss"]},
                },
            ]
        )
    events.extend(
        {
            "event_type": "tool.execution.started",
            "payload": {"args": {"skills": ["vulnerabilities/xss"]}},
        }
        for _ in range(30)
    )
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    project_assignment.write_assignment(run_dir, project["id"], source="test")
    pending_dir = server.state.runs_root / "pending_5678"
    pending_dir.mkdir()
    (pending_dir / "run.json").write_text(
        json.dumps({"status": "running", "scan_config": {"target": "example.com"}}),
        encoding="utf-8",
    )
    project_assignment.write_assignment(pending_dir, project["id"], source="test")

    global_response = project_api.get("/api/analytics/skills")
    project_response = project_api.get(f"/api/projects/{project['id']}/analytics/skills")

    assert global_response.status_code == project_response.status_code == 200
    analytics = global_response.json()
    project_analytics = project_response.json()
    assert analytics["skills"] == project_analytics["skills"]
    assert analytics["categories"] == project_analytics["categories"]
    for key, value in analytics["totals"].items():
        assert value == project_analytics["totals"][key]
    assert [run["run"] for run in analytics["runs"]] == [run_dir.name]
    assert project_analytics["totals"]["project_runs"] == 2
    # The old global handler reported 56: it counted both create_agent's
    # dispatch and its child-created event for each of the 13 injections.
    assert analytics["totals"]["total_loads"] == 43
    assert analytics["totals"]["runs_with_skills"] == 1
    assert analytics["totals"]["distinct_skills"] == 1
    assert analytics["skills"][0]["loads"] == 43
    assert analytics["skills"][0]["runs"] == 1
    assert analytics["categories"][0]["loads"] == 43
    assert analytics["runs"][0]["total"] == 43


@pytest.mark.parametrize(
    ("target", "scan_type", "allowed", "normalized"),
    [
        ("https://API.Example.com/login", "web", True, "api.example.com"),
        ("https://example.com.attacker.test", "web", False, "example.com.attacker.test"),
        ("10.20.2.4", "internal", True, "10.20.2.4"),
        ("10.20.2.0/24", "internal", True, "10.20.2.0/24"),
        ("10.0.0.0/8", "internal", False, "10.0.0.0/8"),
    ],
)
def test_target_preflight_is_read_only(
    project_api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    scan_type: str,
    allowed: bool,
    normalized: str,
) -> None:
    project = _save_project(_domain_scope(), revision=7)
    before = projects_store.projects_path().read_bytes()

    def unexpected_side_effect(*args: object, **kwargs: object) -> None:
        pytest.fail("target preflight must not resolve, launch, or save anything")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_side_effect)
    monkeypatch.setattr(server.subprocess, "Popen", unexpected_side_effect)
    monkeypatch.setattr(projects_store, "save_projects", unexpected_side_effect)

    response = project_api.post(
        f"/api/projects/{project['id']}/validate-target",
        json={"target": target, "scan_type": scan_type},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["allowed"] is allowed
    assert data["normalized_target"] == normalized
    assert data["scope_revision"] == 7
    assert data["reason"]
    assert projects_store.projects_path().read_bytes() == before
    assert list(server.state.runs_root.iterdir()) == []
    assert server.state.scans == {}


def test_target_preflight_allows_valid_target_in_default_scope(project_api: TestClient) -> None:
    project = _save_project()
    response = project_api.post(
        f"/api/projects/{project['id']}/validate-target",
        json={"target": "https://outside.example", "scan_type": "web"},
    )
    assert response.status_code == 200
    assert response.json()["allowed"] is True


@pytest.mark.parametrize("target", ["", "https://user:pass@example.com", "example.com other.test"])
def test_target_preflight_rejects_invalid_target(project_api: TestClient, target: str) -> None:
    project = _save_project()
    response = project_api.post(
        f"/api/projects/{project['id']}/validate-target",
        json={"target": target, "scan_type": "web"},
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


def test_target_preflight_rejects_broken_stored_policy(project_api: TestClient) -> None:
    project = _save_project({"schema_version": 1, "mode": "restricted", "entries": []})
    response = project_api.post(
        f"/api/projects/{project['id']}/validate-target",
        json={"target": "example.com", "scan_type": "web"},
    )
    assert response.status_code == 422
    assert "scope.entries" in response.json()["detail"]


def test_target_preflight_returns_unknown_project(project_api: TestClient) -> None:
    response = project_api.post(
        "/api/projects/prj_missing/validate-target",
        json={"target": "example.com", "scan_type": "web"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "unknown project"


def test_launch_rechecks_scope_after_successful_preflight(project_api: TestClient) -> None:
    project = _save_project()
    body = {"target": "outside.example", "scan_type": "web"}
    response = project_api.post(f"/api/projects/{project['id']}/validate-target", json=body)
    assert response.json()["allowed"] is True

    project["scope"] = _domain_scope()
    project["scope_revision"] += 1
    projects_store.save_projects({"schema_version": 2, "projects": [project]})

    with pytest.raises(HTTPException) as caught:
        server._validated_launch_project(server.ScanBody(**body, project_id=project["id"]))
    assert caught.value.status_code == 422
    assert "outside the project scope" in caught.value.detail
