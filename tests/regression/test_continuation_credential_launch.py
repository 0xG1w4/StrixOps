"""Credential snapshots survive Console and CLI launch boundaries using fake data."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from strixops.config.settings import EngineSettings
from strixops.console import (
    auth_store,
    batch_launch,
    continuation_budget,
    continuation_credentials,
    project_assignment,
    projects_store,
    rerun_context,
    server,
)
from strixops.engine.scanconfig import ScanSpec, build_root_task
from strixops.queue import QueueError, QueueStore
from strixops.queue import cli as queue_cli
from strixops.queue import worker
from strixops.report.credential_store import CredentialStore


@pytest.fixture
def launch_api(tmp_path, monkeypatch):
    for variable, name in {
        "STRIXOPS_AUTH_DB": "auth.sqlite3", "STRIXOPS_CONSOLE_CONFIG": "console.json",
        "STRIXOPS_PROJECTS_FILE": "projects.json", "STRIXOPS_QUEUE_DB": "queue.sqlite3",
        "STRIXOPS_MCP_ROOT": "mcp", "STRIXOPS_FOFA_ROOT": "fofa", "STRIX_RUNS": "runs",
    }.items():
        monkeypatch.setenv(variable, str(tmp_path / name))
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setattr(server, "state", server.ConsoleState(root))
    monkeypatch.setattr(server, "_batch_controller", None)
    monkeypatch.setattr(server, "_batch_controller_key", None)
    monkeypatch.setattr(auth_store, "SCRYPT_N", 1024)
    monkeypatch.setattr(server, "_resolve_llm_env", lambda _: {
        "strix_llm": "fixture-model", "llm_api_base": "https://model.invalid/v1",
        "llm_api_key": "fixture-api-key", "llm_api_mode": "chat_completions",
        "llm_reasoning_effort": "default",
    })
    monkeypatch.setattr(server.web_search_settings, "launch_environment", lambda: {})
    monkeypatch.setattr(batch_launch, "_capture_resources", lambda: {"prompt_parts": {}, "skills": []})
    calls = []

    def popen(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(pid=900001 + len(calls), poll=lambda: None)

    monkeypatch.setattr(batch_launch.subprocess, "Popen", popen)
    monkeypatch.setattr(batch_launch, "process_identity", lambda _: "fixture")
    budget = Mock(return_value={})
    monkeypatch.setattr(continuation_budget, "validate_batch_budget", budget)
    client = TestClient(server.app, base_url="http://testserver", headers={"origin": "http://testserver"})
    login = client.post("/api/auth/login", headers={"X-StrixOps-Request": "1"},
                        json={"username": "strix", "password": "strix123"})
    assert login.status_code == 200, login.text
    changed = client.post("/api/auth/password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                          json={"current_password": "strix123", "new_password": "Meadow Lantern Indigo 49!"})
    assert changed.status_code == 200, changed.text
    client.headers["X-CSRF-Token"] = changed.json()["csrf_token"]
    try:
        yield client, calls, budget
    finally:
        client.close()


def _project(name):
    project, errors = projects_store.sanitize_project({"name": name})
    assert not errors
    data = projects_store.load_projects()
    data["projects"].append(project)
    projects_store.save_projects(data)
    return project["id"]


def _run(name, project_id, *secrets):
    run = server.state.runs_root / name
    run.mkdir()
    (run / "run.json").write_text(json.dumps({
        "status": "completed", "report_synthesized": True,
        "scan_config": {"target": "https://one.invalid"},
    }))
    (run / rerun_context.REPORT).write_text("# Final report\nPrior coverage.\n")
    project_assignment.write_assignment(run, project_id)
    for secret in secrets:
        result = CredentialStore(run).record_credential(
            host="one.invalid", username="fixture-user", password=secret,
            agent_id="root", agent_name="Root",
        )
        assert result["success"]
    return run


def _body(run, project_id, **extra):
    return {
        "target": "https://one.invalid", "rerun_mode": "continue", "project_id": project_id,
        "source_run": run.name,
        "source_report_sha256": hashlib.sha256((run / rerun_context.REPORT).read_bytes()).hexdigest(),
        **extra,
    }


@pytest.mark.parametrize("queued", [False, True])
def test_console_freezes_only_selected_project_and_launches_full_credentials(launch_api, queued):
    client, calls, budget = launch_api
    project, other = _project("Chosen"), _project("Other")
    source = _run("source_1234", project, "same-fake-secret")
    peer = _run("peer_1234", project, "same-fake-secret", "different-fake-secret")
    _run("other_1234", other, "EXCLUDED-OTHER-PROJECT")
    extra = {"targets": ["https://one.invalid", "https://two.invalid"], "max_concurrent": 1} if queued else {}
    response = client.post("/api/scans", json=_body(source, project, **extra))
    assert response.status_code == 200, response.text
    budget.assert_called_once()
    specs, report, _ = budget.call_args.args
    assert all(not spec.project_credentials_file for spec in specs)
    assert "fake-secret" not in report  # Original report budget does not limit credential context.
    if queued:
        assert not calls
        controller = server._queue_controller()
        public = controller.store.get_batch(response.json()["batch_id"])
        assert "fake-secret" not in json.dumps(public)
        item = controller.store.internal_item(public["items"][0]["id"])
        raw = controller.snapshots._path(item["snapshot_ref"]).read_text()
        assert "fake-secret" not in raw
        # Neither live inventories nor the original final report are consulted at dispatch.
        shutil.rmtree(source)
        shutil.rmtree(peer)
        controller.scheduler.tick()
    assert len(calls) == 1
    argv, options = calls[0]
    path = Path(argv[argv.index("--project-credentials-file") + 1])
    markdown = path.read_text()
    assert markdown.count('"same-fake-secret"') == 1
    assert markdown.count('"different-fake-secret"') == 1
    assert "EXCLUDED-OTHER-PROJECT" not in markdown
    assert "included: 2; omitted: 0" in markdown
    assert "fake-secret" not in repr(argv) + repr(options["env"])
    digest = argv[argv.index("--project-credentials-sha256") + 1]
    metadata = json.loads((path.parent / server.LAUNCH_SIDECAR).read_text())["continuation"]
    spec = ScanSpec(
        target="https://one.invalid", previous_report_file=str(path.parent / rerun_context.SNAPSHOT),
        continuation=metadata, project_credentials_file=str(path), project_credentials_sha256=digest,
    )
    assert not spec.validate()
    assert "different-fake-secret" in build_root_task(spec)
    assert "fake-secret" not in json.dumps(spec.as_scan_config())


@pytest.mark.parametrize("mode,assigned", [("new", True), ("continue", False)])
def test_no_credentials_added_to_fresh_or_unassigned_launch(launch_api, mode, assigned):
    client, calls, _ = launch_api
    project = _project("Existing")
    source = _run("source_1234", project, "fixture-credential")
    response = client.post("/api/scans", json=_body(source, project if assigned else "", rerun_mode=mode))
    assert response.status_code == 200, response.text
    assert "--project-credentials-file" not in calls[0][0]
    run = server.state.runs_root / response.json()["run_name"]
    assert not (run / continuation_credentials.SNAPSHOT).exists()


def test_changed_project_uses_destination_membership_only(launch_api):
    client, calls, _ = launch_api
    old, new = _project("Old"), _project("New")
    source = _run("source_1234", old, "old-project-secret")
    _run("peer_1234", new, "new-project-secret")
    response = client.post("/api/scans", json=_body(source, new))
    assert response.status_code == 200, response.text
    argv = calls[0][0]
    markdown = Path(argv[argv.index("--project-credentials-file") + 1]).read_text()
    assert "new-project-secret" in markdown and "old-project-secret" not in markdown


def _attachment(markdown):
    return {"metadata": {"sha256": hashlib.sha256(markdown.encode()).hexdigest(),
                         "snapshot_file": "project_credentials.md"}, "markdown": markdown}


def test_large_queue_attachment_is_not_subject_to_control_json_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_launch, "_MAX_SNAPSHOT", 1024)
    snapshots = batch_launch.LaunchSnapshots(tmp_path / "snapshots")
    markdown = "LONG-FAKE-CREDENTIAL\n" * 10000
    reference = snapshots.save({"continuation": {"project_credentials": _attachment(markdown)}})
    assert snapshots._path(reference).stat().st_size < 1024
    assert snapshots.read(reference)["continuation"]["project_credentials"]["markdown"] == markdown
    path = snapshots.directory / f"{reference}.credentials" / "project_credentials.md"
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("tampered")
    with pytest.raises(QueueError, match="unavailable"):
        snapshots.read(reference)
    snapshots.delete(reference)
    assert not list(snapshots.directory.iterdir())


def test_cli_queue_and_worker_rebase_frozen_files_after_originals_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_launch, "_capture_resources", lambda: {"prompt_parts": {}, "skills": []})
    monkeypatch.setattr(queue_cli, "process_identity", lambda _: "fixture")
    popen = Mock(return_value=SimpleNamespace(pid=900010, poll=lambda: None))
    monkeypatch.setattr(queue_cli.subprocess, "Popen", popen)
    original = tmp_path / "original"
    original.mkdir()
    report = original / "previous_report.md"
    report.write_text("# CLI final report")
    attachment = _attachment("# Credentials\nCLI-FROZEN-CREDENTIAL\n")
    continuation_credentials.materialize(original, attachment)
    spec = ScanSpec(
        target="https://one.invalid", targets=["https://one.invalid", "https://two.invalid"],
        previous_report_file=str(report),
        continuation={"source_run": "original", "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                      "snapshot_file": "previous_report.md"},
        project_credentials_file=str(original / "project_credentials.md"),
        project_credentials_sha256=attachment["metadata"]["sha256"],
    )
    store = QueueStore(tmp_path / "queue.sqlite3")
    controller = queue_cli.CliBatchController(store)
    settings = EngineSettings("", "", "", str(tmp_path / "runs"), "", "", True)
    batch = controller.create(spec, settings, max_concurrent=1)
    shutil.rmtree(original)
    controller.scheduler.tick()
    assert popen.call_count == 1
    item = store.internal_item(store.get_batch(batch["id"])["items"][0]["id"])
    run_dir = tmp_path / "runs" / item["run_name"]
    captured = []

    async def run(copied_spec, _settings):
        captured.append(copied_spec)
        assert copied_spec.project_credentials_file == str(run_dir / "project_credentials.md")
        assert copied_spec.load_project_credentials() == attachment["markdown"]
        assert "CLI-FROZEN-CREDENTIAL" in build_root_task(copied_spec)
        return 0

    monkeypatch.setattr(worker, "QueueStore", lambda: store)
    monkeypatch.setattr(worker, "_run_with_signal_handlers", run)
    monkeypatch.setenv("STRIXOPS_QUEUE_ITEM_TOKEN", item["launch_token"])
    monkeypatch.setattr(sys, "argv", ["worker", "--item", item["id"]])
    assert worker.main() == 0
    assert len(captured) == 1
