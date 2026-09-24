"""Project-wide credential deduplication, provenance, and read-only boundaries."""

from __future__ import annotations

import csv
import io
import json
from itertools import permutations

import pytest
from fastapi.testclient import TestClient

from strixops.console import (
    auth,
    auth_store,
    credentials,
    project_assignment,
    project_credentials,
    projects_store,
    server,
)
from strixops.report.credential_store import CredentialStore

AUTHOR = {"agent_id": "root", "agent_name": "Root agent"}


@pytest.fixture()
def project_api(tmp_path, monkeypatch):
    for name, relative in {
        "STRIXOPS_CONSOLE_CONFIG": "console.json", "STRIXOPS_PROJECTS_FILE": "projects.json",
        "STRIXOPS_QUEUE_DB": "queue.sqlite3", "STRIXOPS_FOFA_ROOT": "fofa", "STRIXOPS_MCP_ROOT": "mcp",
    }.items():
        monkeypatch.setenv(name, str(tmp_path / relative))
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setattr(server, "state", server.ConsoleState(root))
    run = root / "fixture_1234"
    run.mkdir()
    monkeypatch.setattr(auth_store, "SCRYPT_N", 1 << 10)
    store = auth_store.AuthStore(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(auth, "get_store", lambda: store)
    project, errors = projects_store.sanitize_project({"name": "Credential inventory"})
    assert not errors
    projects_store.save_projects({"schema_version": 2, "projects": [project]})
    (run / "run.json").write_text(json.dumps({
        "status": "completed", "scan_config": {"scan_type": "internal"},
    }))
    project_assignment.write_assignment(run, project["id"])
    with TestClient(server.app) as client:
        login = client.post(
            "/api/auth/login", headers={"X-StrixOps-Request": "1"},
            json={"username": "strix", "password": "strix123"},
        )
        assert login.status_code == 200, login.text
        changed = client.post(
            "/api/auth/password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={"current_password": "strix123", "new_password": "Amber meadow lantern 42!"},
        )
        assert changed.status_code == 200, changed.text
        client.headers["X-CSRF-Token"] = changed.json()["csrf_token"]
        yield client, project, run


def add_run(project, name, *, status="running", scan_type="web"):
    run = server.state.runs_root / name
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"status": status, "scan_config": {"scan_type": scan_type}}))
    project_assignment.write_assignment(run, project["id"])
    return run


def register(run, **values):
    result = CredentialStore(run).record_credential(
        **{"host": "db", "username": "root", "password": "exact-secret", **values}, **AUTHOR,
    )
    assert result["success"], result
    return result


def endpoint(project):
    return f"/api/projects/{project['id']}/credentials"


def test_duplicate_credentials_preserve_all_task_evidence_and_conflicting_statuses(project_api):
    client, project, run = project_api
    other = add_run(project, "web_running")
    empty = add_run(project, "empty_failed", status="failed")
    register(run, validation_status="validated", validation_evidence="SSH login succeeded", severity="high")
    register(other, validation_status="failed", validation_evidence="SSH login rejected", severity="critical")
    for directory in (run, other):
        (directory / "vulnerabilities.json").write_text(json.dumps([{
            "id": "vuln-same-id", "title": "Credential discovery",
            "metadata": {"credentials": [{"host": "db", "username": "root", "password": "exact-secret"}]},
        }]))
    before = {str(path): path.read_bytes() for directory in (run, other, empty)
              for path in directory.rglob("*") if path.is_file()}

    response = client.post(endpoint(project) + "/query", json={"limit": 25})
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    data = response.json()
    assert data["total"] == data["overall_total"] == 1
    assert data["run_count"] == 3 and data["contributing_run_count"] == 2
    assert data["source_status"] == "available" and data["warnings"] == []
    row = data["credentials"][0]
    assert row["validation_status"] == "unknown" and row["severity"] == "critical"
    assert row["source_runs"] == sorted([run.name, other.name]) and row["source_count"] == 2
    assert "SSH login succeeded" in row["validation_evidence"]
    assert "SSH login rejected" in row["validation_evidence"]
    assert f"[{run.name}] validation=validated" in row["validation_evidence"]
    assert f"[{other.name}] validation=failed" in row["validation_evidence"]
    assert {source["title"] for source in row["sources"] if source["id"] == "vuln-same-id"} == {
        f"{run.name} · Credential discovery", f"{other.name} · Credential discovery",
    }
    assert data["summary"]["validation_status"]["unknown"] == 1
    assert data["summary"]["validation_status"]["validated"] == 0
    filtered = client.post(endpoint(project) + "/query", json={"validation_status": "validated"}).json()
    assert filtered["total"] == 0 and filtered["overall_total"] == 1
    assert filtered["summary"] == data["summary"]
    after = {str(path): path.read_bytes() for directory in (run, other, empty)
             for path in directory.rglob("*") if path.is_file()}
    assert before == after


@pytest.mark.parametrize("variant", [
    {"host": "other-db"}, {"host": "DB"}, {"username": "Root"},
    {"password": " exact-secret"}, {"password": "exact-secret "},
    {"secret_type": "api_key"}, {"password": "", "hash": "exact-secret", "secret_type": "hash"},
])
def test_only_exact_credential_identity_is_deduplicated(project_api, variant):
    client, project, run = project_api
    other = add_run(project, "task-other")
    register(run)
    register(other)
    register(other, **variant)
    data = client.get(endpoint(project)).json()
    assert data["total"] == data["overall_total"] == 2
    assert sorted(row["source_count"] for row in data["credentials"]) == [1, 2]


@pytest.mark.parametrize("statuses", list(permutations(["validated", "failed", "unverified"])))
def test_three_task_status_conflict_is_unknown_independent_of_source_order(project_api, statuses):
    client, project, _run = project_api
    for name, status in zip(("a", "b", "c"), statuses, strict=True):
        task = add_run(project, name)
        register(task, validation_status=status, validation_evidence=f"{name} authentication result")
    data = client.get(endpoint(project)).json()
    row = data["credentials"][0]
    assert data["total"] == 1 and row["source_count"] == 3
    assert row["validation_status"] == "unknown"
    assert all(f"validation={status}" in row["validation_evidence"] for status in statuses)


def test_inventory_combines_registry_findings_and_legacy_csv_without_generating_topology(project_api):
    client, project, run = project_api
    register(run)
    other = add_run(project, "legacy-internal", scan_type="internal")
    findings = other / "internal_findings"
    findings.mkdir()
    (findings / "finding-1.md").write_text(
        '# Saved account\n\n**Host:** db\n\n## Details\nRecorded account\n\n## Metadata\n```json\n'
        + json.dumps({"credentials": [{"host": "db", "username": "root", "password": "exact-secret"}]})
        + "\n```\n"
    )
    evidence = other / "evidence"
    evidence.mkdir()
    (evidence / "credentials.csv").write_text(
        "host,username,password,hash,source,severity,note\n"
        "db,root,exact-secret,,directory export,high,Legacy account\n"
        "legacy,other,other-secret,,directory export,info,Other account\n"
    )
    record = json.loads((other / "run.json").read_text())
    record["evidence"] = {"files": ["credentials.csv"]}
    (other / "run.json").write_text(json.dumps(record))
    data = client.get(endpoint(project)).json()
    assert data["overall_total"] == 2
    row = next(row for row in data["credentials"] if row["host"] == "db")
    assert set(row["source_runs"]) == {run.name, other.name}
    assert {source["kind"] for source in row["sources"]} >= {
        "credential_register", "finding", "credential_csv",
    }
    assert "Legacy account" in row["note"]
    assert not list(server.state.runs_root.rglob("*topology*"))


def test_membership_is_rechecked_for_query_and_csv_and_unknown_project_is_rejected(project_api):
    client, project, run = project_api
    register(run)
    unrelated = add_run(project, "unrelated")
    register(unrelated, password="other-project-secret")
    project_assignment.write_assignment(unrelated, "different-project")
    unassigned = add_run(project, "unassigned")
    register(unassigned, password="unassigned-secret")
    project_assignment.clear_assignment(unassigned)
    assert client.get(endpoint(project)).json()["total"] == 1
    project_assignment.clear_assignment(run)
    assert client.post(endpoint(project) + "/query", json={}).json()["total"] == 0
    downloaded = client.get(endpoint(project) + ".csv")
    assert downloaded.status_code == 200
    assert list(csv.DictReader(io.StringIO(downloaded.content.decode("utf-8-sig")))) == []
    assert client.get("/api/projects/prj_missing/credentials").status_code == 404
    assert client.post("/api/projects/prj_missing/credentials/query", json={}).status_code == 404
    assert client.get("/api/projects/prj_missing/credentials.csv").status_code == 404


def test_project_query_preserves_auth_csrf_and_validates_pagination(project_api):
    client, project, _run = project_api
    route = endpoint(project)
    assert client.post(route + "/query", json={}, headers={"X-CSRF-Token": ""}).status_code == 403
    for body in ({"limit": 101}, {"offset": -1}, {"query": "x" * 501}, {"validation_status": "ok"}):
        assert client.post(route + "/query", json=body).status_code == 422
    client.cookies.clear()
    for url in (route, route + ".csv"):
        assert client.get(url).status_code == 401
    assert client.post(route + "/query", json={}).status_code == 401


def test_source_failures_remain_partial_and_preserve_readable_findings(project_api):
    client, project, run = project_api
    (run / ".state").mkdir()
    (run / ".state" / "credentials.sqlite3").write_bytes(b"broken database")
    (run / "vulnerabilities.json").write_text(json.dumps([{
        "id": "v1", "host": "db", "username": "root", "password": "readable-secret",
    }]))
    response = client.get(endpoint(project))
    data = response.json()
    assert data["source_status"] == "partial"
    assert "credential_register_unreadable" in data["warnings"]
    assert data["credentials"][0]["password"] == "readable-secret"
    exported = client.get(endpoint(project) + ".csv")
    assert exported.headers["x-credential-source-status"] == "partial"
    (run / "vulnerabilities.json").unlink()
    (run / "run.json").write_bytes(b"broken run")
    assert client.get(endpoint(project)).json()["source_status"] == "unreadable"
    assert client.get(endpoint(project) + ".csv").status_code == 503


def test_late_register_read_failure_retains_already_read_rows_and_supplemental(project_api, monkeypatch):
    client, project, run = project_api
    register(run)
    (run / "vulnerabilities.json").write_text(json.dumps([{
        "id": "v1", "host": "legacy", "password": "supplemental-secret",
    }]))
    original = CredentialStore.iter_credentials

    def broken_stream(self, **kwargs):
        yield from original(self, **kwargs)
        raise ValueError("read failed after a row")

    monkeypatch.setattr(CredentialStore, "iter_credentials", broken_stream)
    data = client.get(endpoint(project)).json()
    assert data["source_status"] == "partial" and data["total"] == 2
    assert {row["password"] for row in data["credentials"]} == {"exact-secret", "supplemental-secret"}


def test_large_project_inventory_deduplicates_before_search_pagination_and_full_csv(project_api):
    client, project, run = project_api
    other = add_run(project, "overlapping-task")
    count = 6007  # Beyond legacy register cap, report sample size, and API page cap.
    special = '=literal,"secret\nsecond line'

    def records(start, end):
        for index in range(start, end):
            yield {"host": "db", "username": f"user-{index:05d}",
                   "password": special if index == count - 1 else f"secret-{index}"}

    first = CredentialStore(run).import_rows(records(0, count), source="Authorized export", **AUTHOR)
    overlap = CredentialStore(other).import_rows(records(0, 101), source="Second task export", **AUTHOR)
    assert first["success"] and overlap["success"]
    data = client.post(endpoint(project) + "/query", json={"limit": 25, "offset": count - 10}).json()
    assert data["total"] == data["overall_total"] == count and len(data["credentials"]) == 10
    assert not data["has_more"]
    assert data["summary"]["validation_status"]["unverified"] == count
    found = client.post(endpoint(project) + "/query", json={"query": special})
    assert found.status_code == 200 and not found.request.url.query
    assert found.json()["total"] == 1 and found.json()["overall_total"] == count
    assert found.json()["credentials"][0]["username"] == f"user-{count - 1:05d}"
    assert client.post(endpoint(project) + "/query", json={"query": other.name}).json()["total"] == 101
    exported = client.get(endpoint(project) + ".csv")
    assert "no-store" in exported.headers["cache-control"]
    assert exported.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert len(rows) == count and list(rows[0]) == list(credentials.CSV_FIELDS)
    assert rows[-1]["password"] == "'" + special
    assert run.name in rows[0]["source"] and other.name in rows[0]["source"]


def test_request_inventory_closes_on_success_and_failure(project_api, monkeypatch):
    _client, _project, run = project_api
    with project_credentials.ProjectCredentialInventory([run], server._open_run_file) as inventory:
        assert not inventory.closed
    assert inventory.closed
    closed = []
    original_close = project_credentials.ProjectCredentialInventory.close

    def close(self):
        original_close(self)
        closed.append(self.closed)

    def failing_load(*_args, **_kwargs):
        raise RuntimeError("construction interrupted")

    monkeypatch.setattr(project_credentials.ProjectCredentialInventory, "close", close)
    monkeypatch.setattr(project_credentials.ProjectCredentialInventory, "_load", failing_load)
    with pytest.raises(RuntimeError, match="construction interrupted"):
        project_credentials.ProjectCredentialInventory([run], server._open_run_file)
    assert closed == [True]


@pytest.mark.asyncio
async def test_csv_disconnect_closes_temporary_inventory(project_api, monkeypatch):
    _client, project, run = project_api
    register(run)
    original = project_credentials.ProjectCredentialInventory
    created = []

    def build(*args, **kwargs):
        inventory = original(*args, **kwargs)
        created.append(inventory)
        return inventory

    monkeypatch.setattr(project_credentials, "ProjectCredentialInventory", build)
    response = server.project_credentials_csv(project["id"])
    assert len(created) == 1 and not created[0].closed

    async def disconnected_send(message):
        if message["type"] == "http.response.body":
            raise RuntimeError("client disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(RuntimeError, match="client disconnected"):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, disconnected_send)
    assert created[0].closed
