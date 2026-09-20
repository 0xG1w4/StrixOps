"""Settings/project writes preserve concurrent edits, secrets and stale-edit boundaries.

These tests use temporary local files and direct API handlers. They do not launch
the Console, contact a provider, or depend on other repository test helpers.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.responses import Response

from strixops.console import projects_store, settings_store
from strixops.console.json_store import StoreError


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, relative in {
        "STRIXOPS_CONSOLE_CONFIG": "console.json",
        "STRIXOPS_PROJECTS_FILE": "projects.json",
        "STRIXOPS_AUTH_DB": "auth.sqlite3",
        "STRIXOPS_QUEUE_DB": "queue.sqlite3",
        "STRIXOPS_FOFA_ROOT": "fofa",
        "STRIXOPS_MCP_ROOT": "mcp",
        "STRIX_RUNS": "runs",
    }.items():
        monkeypatch.setenv(name, str(tmp_path / relative))
    for name in tuple(os.environ):
        if name.startswith("PERPLEXITY_"):
            monkeypatch.delenv(name)


@pytest.fixture(params=["settings", "projects"])
def store(request: pytest.FixtureRequest) -> SimpleNamespace:
    if request.param == "settings":
        return SimpleNamespace(
            name="settings", collection="profiles", path=settings_store.config_path(),
            load=settings_store.load_settings, save=settings_store.save_settings,
            transaction=settings_store.settings_transaction,
        )
    return SimpleNamespace(
        name="projects", collection="projects", path=projects_store.projects_path(),
        load=projects_store.load_projects, save=projects_store.save_projects,
        transaction=projects_store.projects_transaction,
    )


def _seed_counter(store: SimpleNamespace) -> None:
    with store.transaction() as data:
        data[store.collection].append({"id": "counter", "left": 0, "right": 0})


def test_transaction_normalizes_missing_store_and_commits_once(store: SimpleNamespace) -> None:
    assert not store.path.exists()
    with store.transaction() as data:
        assert data[store.collection] == []
        data[store.collection].append({"id": "first", "name": "Saved"})
        assert not store.path.exists()
    assert store.load()[store.collection][0]["name"] == "Saved"


@pytest.mark.parametrize("already_exists", [False, True])
def test_exception_discards_transaction_without_creating_or_rewriting_store(
    store: SimpleNamespace, already_exists: bool,
) -> None:
    if already_exists:
        _seed_counter(store)
    original = store.path.read_bytes() if already_exists else None

    with pytest.raises(ValueError, match="invalid proposed edit"), store.transaction() as data:
        data[store.collection].append({"id": "must-not-persist"})
        raise ValueError("invalid proposed edit")

    assert (store.path.read_bytes() if store.path.exists() else None) == original


def test_thread_transactions_preserve_both_incremental_updates(store: SimpleNamespace) -> None:
    _seed_counter(store)
    start = threading.Barrier(2)

    def increment(field: str) -> None:
        start.wait(timeout=5)
        for _ in range(20):
            with store.transaction() as data:
                counter = data[store.collection][0]
                previous = counter[field]
                time.sleep(0.001)  # Widen the lost-update window while the transaction owns its lock.
                counter[field] = previous + 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(increment, field) for field in ("left", "right")]
        for future in futures:
            future.result(timeout=10)

    counter = store.load()[store.collection][0]
    assert (counter["left"], counter["right"]) == (20, 20)


_PROCESS_UPDATES = """
import importlib, pathlib, sys, time
kind, field, ready, gate = sys.argv[1:]
module = importlib.import_module('strixops.console.' + kind + '_store')
transaction = getattr(module, kind + '_transaction')
collection = 'profiles' if kind == 'settings' else 'projects'
pathlib.Path(ready).write_text('ready')
deadline = time.monotonic() + 10
while not pathlib.Path(gate).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('parent did not release process gate')
    time.sleep(0.005)
for _ in range(12):
    with transaction() as data:
        counter = data[collection][0]
        previous = counter[field]
        time.sleep(0.003)
        counter[field] = previous + 1
"""


def test_process_transactions_preserve_both_incremental_updates(
    store: SimpleNamespace, tmp_path: Path,
) -> None:
    _seed_counter(store)
    gate = tmp_path / "start-processes"
    ready = [tmp_path / f"{field}.ready" for field in ("left", "right")]
    children: list[subprocess.Popen[str]] = []
    try:
        for field, marker in zip(("left", "right"), ready, strict=True):
            children.append(subprocess.Popen(
                [sys.executable, "-c", _PROCESS_UPDATES, store.name, field, str(marker), str(gate)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 10
        while not all(marker.exists() for marker in ready):
            assert all(child.poll() is None for child in children), "transaction worker exited early"
            assert time.monotonic() < deadline, "transaction workers did not become ready"
            time.sleep(0.01)
        gate.write_text("start", encoding="utf-8")
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stdout + stderr
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    counter = store.load()[store.collection][0]
    assert (counter["left"], counter["right"]) == (12, 12)


def test_parallel_legacy_save_never_collides_on_temporary_filename(store: SimpleNamespace) -> None:
    start = threading.Barrier(4)

    def write_snapshot(index: int) -> None:
        payload = {store.collection: [{"id": str(index), "name": "fixture" * 1000}]}
        for _ in range(10):
            start.wait(timeout=5)
            store.save(payload)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(write_snapshot, index) for index in range(4)]
        for future in futures:
            future.result(timeout=15)

    rows = store.load()[store.collection]
    assert len(rows) == 1 and rows[0]["id"] in {"0", "1", "2", "3"}
    assert rows[0]["name"] == "fixture" * 1000


@pytest.mark.parametrize("payload", [b'{"private":', b"[]", b"null", b'"secret fixture"', b"\xff"])
def test_corrupt_or_nonobject_store_rejects_reads_and_all_writes(
    store: SimpleNamespace, payload: bytes,
) -> None:
    store.path.write_bytes(payload)
    with pytest.raises(StoreError):
        store.load()
    with pytest.raises(StoreError), store.transaction() as data:
        data[store.collection] = []
    with pytest.raises(StoreError):
        store.save({store.collection: []})
    assert store.path.read_bytes() == payload


def test_store_read_error_is_not_treated_as_empty_data(store: SimpleNamespace) -> None:
    store.path.mkdir()
    with pytest.raises(StoreError):
        store.load()
    with pytest.raises(StoreError):
        store.save({store.collection: []})
    assert store.path.is_dir()


def test_temporary_and_final_files_are_private_before_atomic_publication(
    store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open, original_replace = os.open, os.replace
    created_modes, published_modes = [], []

    def check_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if str(path).endswith(".tmp") and flags & os.O_CREAT:
            created_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    def check_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        published_modes.append(stat.S_IMODE(os.stat(src, dir_fd=src_dir_fd).st_mode))
        return original_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "open", check_open)
    monkeypatch.setattr(os, "replace", check_replace)
    _seed_counter(store)
    assert created_modes and set(created_modes) == {0o600}
    assert published_modes and set(published_modes) == {0o600}
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_failed_atomic_replace_preserves_original_and_removes_temporary_file(
    store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_counter(store)
    original = store.path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(StoreError), store.transaction() as data:
        data[store.collection][0]["left"] = 99
    assert store.path.read_bytes() == original
    assert not list(store.path.parent.glob("*.tmp"))


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from strixops.console import server

    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    monkeypatch.setattr(server, "state", server.ConsoleState(runs))
    return server


def _create_profile(api) -> dict:
    return api.create_profile(api.ProfileBody(
        name="Primary", llm_api_base="https://provider.invalid/v1",
        llm_api_key="fixture-api-key", model_web="fixture-model",
    ))["profile"]


def test_profile_update_rejects_stale_revision_without_changing_bytes(api) -> None:
    created = _create_profile(api)
    assert created["revision"] == 1
    updated = api.update_profile(created["id"], api.ProfileBody(
        name="First editor", expected_revision=created["revision"],
    ))["profile"]
    assert updated["revision"] == 2
    original = settings_store.config_path().read_bytes()

    with pytest.raises(HTTPException) as caught:
        api.update_profile(created["id"], api.ProfileBody(
            name="Stale editor", expected_revision=created["revision"],
        ))
    assert caught.value.status_code == 409
    assert settings_store.config_path().read_bytes() == original
    public = api.get_settings()["profiles"][0]
    assert public["revision"] == 2 and public["name"] == "First editor"
    assert "fixture-api-key" not in json.dumps(public)


def test_project_update_rejects_stale_revision_and_preserves_omitted_fields(api) -> None:
    created = api.create_project(api.ProjectBody(
        name="Primary", description="Keep this description", color="cyan",
    ))["project"]
    assert created["revision"] == 1
    updated = api.update_project(created["id"], api.ProjectBody(
        name="First editor", expected_revision=1,
    ))["project"]
    assert updated["revision"] == 2
    assert updated["description"] == "Keep this description" and updated["color"] == "cyan"
    original = projects_store.projects_path().read_bytes()

    with pytest.raises(HTTPException) as caught:
        api.update_project(created["id"], api.ProjectBody(name="Stale editor", expected_revision=1))
    assert caught.value.status_code == 409
    assert projects_store.projects_path().read_bytes() == original
    assert api.list_projects()["projects"][0]["revision"] == 2


@pytest.mark.parametrize("kind", ["profile", "project"])
def test_legacy_record_is_public_revision_zero_and_first_update_advances_to_one(api, kind: str) -> None:
    if kind == "profile":
        created = _create_profile(api)
        data = settings_store.load_settings()
        data["profiles"][0].pop("revision")
        settings_store.save_settings(data)
        path = settings_store.config_path()

        def public():
            return api.get_settings()["profiles"][0]

        def update():
            return api.update_profile(created["id"], api.ProfileBody(
                name="Migrated", expected_revision=0,
            ))["profile"]
    else:
        created = api.create_project(api.ProjectBody(name="Legacy"))["project"]
        data = projects_store.load_projects()
        data["projects"][0].pop("revision")
        projects_store.save_projects(data)
        path = projects_store.projects_path()

        def public():
            return api.list_projects()["projects"][0]

        def update():
            return api.update_project(created["id"], api.ProjectBody(
                name="Migrated", expected_revision=0,
            ))["project"]
    original = path.read_bytes()
    assert public()["revision"] == 0
    assert path.read_bytes() == original  # Reading a legacy store never performs a migration write.
    assert update()["revision"] == 1


@pytest.mark.parametrize("kind", ["profile", "project"])
def test_concurrent_api_edits_with_same_revision_have_one_winner(api, kind: str) -> None:
    created = _create_profile(api) if kind == "profile" else api.create_project(
        api.ProjectBody(name="Primary")
    )["project"]
    start = threading.Barrier(2)

    def edit(name: str) -> tuple[int, dict | None]:
        start.wait(timeout=5)
        try:
            if kind == "profile":
                result = api.update_profile(created["id"], api.ProfileBody(name=name, expected_revision=1))
            else:
                result = api.update_project(created["id"], api.ProjectBody(name=name, expected_revision=1))
            return 200, result[kind]
        except HTTPException as exc:
            return exc.status_code, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ("Left editor", "Right editor")))
    assert sorted(status for status, _ in results) == [200, 409]
    winner = next(record for status, record in results if status == 200)
    assert winner["revision"] == 2
    stored = api.get_settings()["profiles"][0] if kind == "profile" else api.list_projects()["projects"][0]
    assert stored["name"] == winner["name"] and stored["revision"] == 2


def test_integration_update_rejects_stale_revision_and_retains_profiles(api) -> None:
    profile = _create_profile(api)
    assert api.get_integrations(Response())["revision"] == 0
    updated = api.set_integrations(api.IntegrationBody(
        perplexity_api_key="fixture-search-key", perplexity_enabled=True, expected_revision=0,
    ), Response())
    assert updated["revision"] == 1
    assert "fixture-search-key" not in json.dumps(updated)
    original = settings_store.config_path().read_bytes()

    with pytest.raises(HTTPException) as caught:
        api.set_integrations(api.IntegrationBody(perplexity_enabled=False, expected_revision=0), Response())
    assert caught.value.status_code == 409
    assert settings_store.config_path().read_bytes() == original
    assert api.get_settings()["profiles"][0]["id"] == profile["id"]
    assert api.get_integrations(Response())["revision"] == 1


def test_scope_update_rejects_stale_project_revision_without_changing_policy(api) -> None:
    project = api.create_project(api.ProjectBody(name="Portal"))["project"]
    updated = api.update_project_scope(project["id"], api.ProjectScopeBody(
        scope_rules=[{"kind": "domain", "value": "example.com", "include_subdomains": True}],
        expected_revision=project["revision"],
    ))["project"]
    assert updated["revision"] == 2 and updated["scope_revision"] == 2
    original = projects_store.projects_path().read_bytes()

    with pytest.raises(HTTPException) as caught:
        api.update_project_scope(project["id"], api.ProjectScopeBody(
            scope_rules=[{"kind": "any", "value": "*"}], expected_revision=1,
        ))
    assert caught.value.status_code == 409
    assert projects_store.projects_path().read_bytes() == original
    assert projects_store.load_projects()["projects"][0]["scope"]["mode"] == "restricted"


@pytest.mark.parametrize("kind", ["settings", "projects"])
def test_corrupt_store_returns_safe_http_error_and_preserves_data(api, kind, monkeypatch) -> None:
    from strixops.console import auth_store

    monkeypatch.setattr(auth_store, "SCRYPT_N", 1024)
    with TestClient(api.app) as client:
        login = client.post("/api/auth/login", json={"username": "strix", "password": "strix123"},
                            headers={"X-StrixOps-Request": "1"})
        assert login.status_code == 200
        changed = client.post("/api/auth/password", json={
            "current_password": "strix123", "new_password": "Meadow Lantern Indigo 49!",
        }, headers={"X-CSRF-Token": login.json()["csrf_token"]})
        assert changed.status_code == 200
        client.headers["X-CSRF-Token"] = changed.json()["csrf_token"]
        path = settings_store.config_path() if kind == "settings" else projects_store.projects_path()
        original = b'{"fixture-private-key": '
        path.write_bytes(original)
        read_response = client.get(f"/api/{kind}")
        write_response = client.post(
            "/api/settings/profiles" if kind == "settings" else "/api/projects",
            json={"name": "Must not replace damaged settings"},
        )
        for response in (read_response, write_response):
            assert response.status_code == 503
            assert response.json()["error_code"] == "storage_unavailable"
            assert response.headers["cache-control"] == "no-store"
            assert "fixture-private-key" not in response.text and str(path) not in response.text
        assert path.read_bytes() == original
