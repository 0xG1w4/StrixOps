"""Continuation preserves full references without offline capacity admission checks."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from strixops.console import continuation_budget, continuation_credentials, rerun_context, server
from strixops.engine.scanconfig import ScanSpec, build_root_task
from tests.regression.test_continuation_credential_launch import (
    _body,
    _project,
    _run,
    launch_api as launch_api,
)


@pytest.mark.parametrize("queued", [False, True])
def test_tiny_offline_capacity_does_not_block_full_reference_launch(launch_api, monkeypatch, queued):
    client, calls, budget = launch_api
    monkeypatch.setenv("STRIX_CONTEXT_FALLBACK_TOKENS", "1")
    capacity = Mock(return_value=(1, 1, "configured_fallback"))
    monkeypatch.setattr(continuation_budget, "_capacity", capacity)
    budget.side_effect = rerun_context.ContinuationError("context_budget_exceeded")
    project = _project("Full references")
    secret = "FAKE-CREDENTIAL-" + "x" * 32_000
    source = _run("source_1234", project, secret)
    report_bytes = (
        b"# Complete report\r\nBEGIN\r\n"
        + "MIDDLE-合成報告|unabridged coverage\r\n".encode() * 50_000
        + b"END\r\n"
    )
    (source / rerun_context.REPORT).write_bytes(report_bytes)
    extra = {"targets": ["https://one.invalid", "https://two.invalid"], "max_concurrent": 1} if queued else {}
    response = client.post("/api/scans", json=_body(
        source, project, instruction="Current authorized scope", additional_instruction="Retest coverage",
        **extra,
    ))
    assert response.status_code == 200, response.text
    budget.assert_not_called()
    capacity.assert_not_called()
    if queued:
        assert not calls
        (source / rerun_context.REPORT).unlink()
        server._queue_controller().scheduler.tick()
    assert len(calls) == 1
    argv = calls[0][0]
    report_file = Path(argv[argv.index("--previous-report-file") + 1])
    credentials_file = Path(argv[argv.index("--project-credentials-file") + 1])
    assert report_file.read_bytes() == report_bytes
    credentials = credentials_file.read_text(encoding="utf-8")
    assert secret in credentials
    metadata = json.loads((report_file.parent / server.LAUNCH_SIDECAR).read_text())["continuation"]
    spec = ScanSpec(
        target="https://one.invalid", previous_report_file=str(report_file), continuation=metadata,
        project_credentials_file=str(credentials_file),
        project_credentials_sha256=argv[argv.index("--project-credentials-sha256") + 1],
    )
    assert spec.load_previous_report().encode() == report_bytes
    root = build_root_task(spec)
    assert json.dumps(report_bytes.decode(), ensure_ascii=False) in root
    assert json.dumps(credentials, ensure_ascii=False) in root
    instructions = (report_file.parent / "instruction.md").read_text()
    assert "Current authorized scope" in instructions and "Retest coverage" in instructions
    assert "MIDDLE-" not in instructions and secret not in instructions


@pytest.mark.parametrize("change,expected", [
    ("changed", "report_changed"),
    ("missing", "report_missing"),
    ("empty", "report_empty"),
    ("symlink", "report_unreadable"),
    ("encoding", "report_unreadable"),
    ("oversized", "report_too_large"),
    ("draft", "report_not_final"),
    ("running", "task_active"),
    ("finalizing", "task_active"),
    ("owned_process", "task_active"),
])
def test_source_integrity_still_blocks_before_launch(launch_api, tmp_path, change, expected):
    client, calls, budget = launch_api
    project = _project("Source checks")
    source = _run("source_1234", project, "FAKE-SOURCE-CREDENTIAL")
    request = _body(source, project)
    report = source / rerun_context.REPORT
    if change == "changed":
        report.write_text("# A different final report")
    elif change == "missing":
        report.unlink()
    elif change == "empty":
        report.write_text(" \r\n\t")
    elif change == "symlink":
        other = tmp_path / "external-fixture.md"
        other.write_text("# Must not follow this link")
        report.unlink()
        report.symlink_to(other)
    elif change == "encoding":
        report.write_bytes(b"\xff\xfe")
    elif change == "oversized":
        report.write_bytes(b"x" * (rerun_context.MAX_REPORT_BYTES + 1))
    elif change == "owned_process":
        server.state.scans["owned-fixture"] = {"run_name": source.name, "popen": Mock(poll=lambda: None)}
    else:
        record = json.loads((source / "run.json").read_text())
        record.update({
            "draft": {"report_synthesized": False},
            "running": {"status": "running"},
            "finalizing": {"cleanup": {"status": "in_progress"}},
        }[change])
        (source / "run.json").write_text(json.dumps(record))
    response = client.post("/api/scans", json=request)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == expected
    assert not calls
    assert list(server.state.runs_root.iterdir()) == [source]
    assert server._queue_controller().store.list_batches()["total"] == 0
    budget.assert_not_called()


def test_new_mode_ignores_stale_source_and_does_not_add_references(launch_api, monkeypatch):
    client, calls, budget = launch_api
    project = _project("Fresh launch")
    source = _run("source_1234", project, "FAKE-OLD-CREDENTIAL")
    request = _body(source, project, rerun_mode="new")
    (source / rerun_context.REPORT).unlink()
    snapshot = Mock(side_effect=AssertionError("New tasks must not load project credentials"))
    monkeypatch.setattr(continuation_credentials, "build_snapshot", snapshot)
    response = client.post("/api/scans", json=request)
    assert response.status_code == 200, response.text
    argv = calls[0][0]
    assert "--previous-report-file" not in argv and "--project-credentials-file" not in argv
    run = server.state.runs_root / response.json()["run_name"]
    assert not (run / rerun_context.SNAPSHOT).exists()
    assert not (run / continuation_credentials.SNAPSHOT).exists()
    snapshot.assert_not_called()
    budget.assert_not_called()
