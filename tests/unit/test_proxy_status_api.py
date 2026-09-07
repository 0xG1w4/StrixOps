"""Proxy status routes use run liveness/configuration without changing the run."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from strixops.console import server


@pytest.fixture()
def proxy_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(server, "state", server.ConsoleState(runs_root))
    monkeypatch.setattr(server, "_pid_alive", Mock(return_value=False))
    with TestClient(server.app) as client:
        yield client


@pytest.fixture()
def status_reader(monkeypatch: pytest.MonkeyPatch) -> Mock:
    reader = Mock(return_value={"status": "running", "captured_requests": 7})
    monkeypatch.setattr(server.proxy_status, "get_proxy_status", reader)
    return reader


def _make_run(
    *, status: str = "running", scan_type: str = "web", dry_run: bool | None = None, stale: bool = False
) -> Path:
    run_dir = server.state.runs_root / "example-com_abcd"
    run_dir.mkdir()
    config: dict = {"target": "https://example.com", "scan_type": scan_type}
    if dry_run is not None:
        config["dry_run"] = dry_run
    (run_dir / "run.json").write_text(json.dumps({"status": status, "scan_config": config}), encoding="utf-8")
    events = run_dir / "events.jsonl"
    events.write_text("", encoding="utf-8")
    if stale:
        os.utime(events, (1, 1))
    return run_dir


def test_unknown_run_is_404_before_proxy_lookup(proxy_api: TestClient, status_reader: Mock) -> None:
    response = proxy_api.get("/api/runs/missing-run/proxy")

    assert response.status_code == 404
    status_reader.assert_not_called()


def test_live_web_run_delegates_read_only(
    proxy_api: TestClient, status_reader: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run()
    before = {path.name: path.read_bytes() for path in run_dir.iterdir()}
    launch = Mock(side_effect=AssertionError("a status read must not launch a process"))
    monkeypatch.setattr(server.subprocess, "Popen", launch)

    response = proxy_api.get(f"/api/runs/{run_dir.name}/proxy")

    assert response.status_code == 200
    assert response.json() == status_reader.return_value
    status_reader.assert_called_once_with(run_dir, live=True, enabled=True)
    assert {path.name: path.read_bytes() for path in run_dir.iterdir()} == before
    assert server.state.scans == {}
    launch.assert_not_called()


@pytest.mark.parametrize(("status", "stale"), [("completed", False), ("failed", False), ("running", True)])
def test_stopped_or_stale_run_is_not_probed_as_live(
    proxy_api: TestClient, status_reader: Mock, status: str, stale: bool
) -> None:
    run_dir = _make_run(status=status, stale=stale)

    response = proxy_api.get(f"/api/runs/{run_dir.name}/proxy")

    assert response.status_code == 200
    status_reader.assert_called_once_with(run_dir, live=False, enabled=True)


@pytest.mark.parametrize(("scan_type", "dry_run"), [("internal", False), ("web", True), ("internal", True)])
def test_internal_and_dry_runs_disable_caido(
    proxy_api: TestClient, status_reader: Mock, scan_type: str, dry_run: bool
) -> None:
    run_dir = _make_run(scan_type=scan_type, dry_run=dry_run)

    response = proxy_api.get(f"/api/runs/{run_dir.name}/proxy")

    assert response.status_code == 200
    status_reader.assert_called_once_with(run_dir, live=True, enabled=False)


def test_dry_run_launch_metadata_also_disables_caido(proxy_api: TestClient, status_reader: Mock) -> None:
    run_dir = _make_run()
    (run_dir / server.LAUNCH_SIDECAR).write_text(json.dumps({"dry_run": True}), encoding="utf-8")

    response = proxy_api.get(f"/api/runs/{run_dir.name}/proxy")

    assert response.status_code == 200
    status_reader.assert_called_once_with(run_dir, live=True, enabled=False)
