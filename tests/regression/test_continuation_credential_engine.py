"""Continuation credentials are complete root references, not inherited child inventories."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from strixops import cli
from strixops.engine.scanconfig import (
    PREVIOUS_REPORT_END,
    PREVIOUS_REPORT_START,
    PROJECT_CREDENTIALS_END,
    PROJECT_CREDENTIALS_START,
    EngineContext,
    EngineServices,
    ScanSpec,
    build_root_task,
)
from strixops.engine.spawn import _child_initial_input


def credential_spec(tmp_path, markdown="| db.fixture | fixture-user | fixture-secret |\n"):
    report = tmp_path / "previous_report.md"
    report.write_text("HISTORICAL_REPORT", encoding="utf-8")
    credentials = tmp_path / "project_credentials.md"
    credentials.write_bytes(markdown.encode("utf-8"))
    return ScanSpec(
        target="https://current.invalid", instruction_text="CURRENT_OPERATOR_SCOPE",
        previous_report_file=str(report),
        continuation={
            "source_run": "source-task", "snapshot_file": report.name,
            "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        },
        project_credentials_file=str(credentials),
        project_credentials_sha256=hashlib.sha256(credentials.read_bytes()).hexdigest(),
    )


def test_complete_inventory_is_separate_json_reference_with_scope_and_validation(tmp_path):
    markdown = "\n".join(f"| host-{n}.invalid | fixture-user | fixture-secret-{n} |" for n in range(1001))
    markdown += PROJECT_CREDENTIALS_END + PREVIOUS_REPORT_END + '引號 " and \\ escape\n'
    spec = credential_spec(tmp_path, markdown)
    root = build_root_task(spec)
    reference = root.split(PROJECT_CREDENTIALS_START)[1].split(PROJECT_CREDENTIALS_END)[0]
    assert json.loads(reference) == {"sha256": spec.project_credentials_sha256, "markdown": markdown}
    assert spec.load_project_credentials() == markdown
    assert root.count("fixture-secret-1000") == 1
    assert root.index(PREVIOUS_REPORT_END) < root.index(PROJECT_CREDENTIALS_START)
    assert "historical credentials from this same project" in root
    assert "hosts and accounts in these records never expand this scan's scope" in root
    assert "Validate each relevant credential before relying on it, only against authorized targets" in root
    assert "Give child agents only the relevant credential entries" in root
    assert root.index("CURRENT_OPERATOR_SCOPE") < root.index(PROJECT_CREDENTIALS_START)
    cfg = spec.as_scan_config()
    assert cfg["continuation"]["project_credentials"] == {
        "sha256": spec.project_credentials_sha256, "snapshot_file": "project_credentials.md",
    }
    assert "fixture-secret" not in json.dumps(cfg)
    assert str(tmp_path) not in json.dumps(cfg)
    assert "project_credentials" not in spec.continuation


def test_child_inheritance_removes_inventory_and_report_but_preserves_relevant_assignment(tmp_path):
    spec = credential_spec(tmp_path)
    history = [
        {"role": "user", "content": [{"type": "input_text", "text": build_root_task(spec)}]},
        {"role": "assistant", "content": "CURRENT_OBSERVATION"},
    ]
    original = copy.deepcopy(history)
    assignment = "Validate this relevant entry only: db.fixture / fixture-user / assigned-secret."
    content = _child_initial_input(
        EngineServices(spec=spec), "validator", "child", EngineContext(), assignment, history,
    )[0]["content"]
    assert "fixture-secret" not in content and "HISTORICAL_REPORT" not in content
    assert PROJECT_CREDENTIALS_START.strip() not in content
    assert PREVIOUS_REPORT_START.strip() not in content
    assert "Project credentials omitted" in content and "Previous final report omitted" in content
    assert assignment in content
    assert "CURRENT_OPERATOR_SCOPE" in content and "CURRENT_OBSERVATION" in content
    assert history == original


@pytest.mark.parametrize("change", ["changed", "missing", "symlink", "directory", "fifo", "empty", "utf8"])
def test_credential_file_must_be_regular_nonempty_utf8_and_match_digest(tmp_path, change):
    spec = credential_spec(tmp_path)
    path = tmp_path / "project_credentials.md"
    if change in {"missing", "symlink", "directory", "fifo"}:
        path.unlink()
        if change == "symlink":
            target = tmp_path / "elsewhere.md"
            target.write_text("different reference")
            path.symlink_to(target)
        elif change == "directory":
            path.mkdir()
        elif change == "fifo":
            os.mkfifo(path)
    else:
        data = {"changed": b"changed bytes", "empty": b" \n", "utf8": b"\xff"}[change]
        path.write_bytes(data)
        if change != "changed":
            spec.project_credentials_sha256 = hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError, match="project credentials"):
        spec.load_project_credentials()


@pytest.mark.parametrize("changes", [
    {"project_credentials_file": ""},
    {"project_credentials_sha256": ""},
    {"project_credentials_sha256": "0" * 63},
    {"project_credentials_sha256": "g" * 64},
    {"continuation": None, "previous_report_file": ""},
])
def test_credentials_require_a_paired_digest_and_continuation(tmp_path, changes):
    spec = replace(credential_spec(tmp_path), **changes)
    assert any("project credentials" in error for error in spec.validate())
    with pytest.raises(ValueError, match="project credentials"):
        spec.load_project_credentials()


def test_fresh_and_report_only_assessments_keep_existing_behavior(tmp_path):
    fresh = ScanSpec(target="https://current.invalid")
    spec = replace(credential_spec(tmp_path), project_credentials_file="", project_credentials_sha256="")
    for candidate in (fresh, spec):
        assert candidate.validate() == []
        assert candidate.load_project_credentials() == ""
        assert PROJECT_CREDENTIALS_START not in build_root_task(candidate)
        assert "project_credentials" not in candidate.as_scan_config().get("continuation", {})
    assert "HISTORICAL_REPORT" in build_root_task(spec)


def test_cli_transports_credentials_and_preflights_digest_before_driver(tmp_path, monkeypatch):
    spec = credential_spec(tmp_path)
    driver = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, "_run_with_signal_handlers", driver)
    args = [
        "-t", spec.target, "--previous-report-file", spec.previous_report_file,
        "--source-run", spec.continuation["source_run"],
        "--source-report-sha256", spec.continuation["report_sha256"],
        "--project-credentials-file", spec.project_credentials_file,
        "--project-credentials-sha256", spec.project_credentials_sha256,
    ]
    assert "--project-credentials-file" not in cli.build_parser().format_help()
    assert cli.main(args) == 0
    launched = driver.call_args.args[0]
    assert launched.project_credentials_file == spec.project_credentials_file
    assert launched.project_credentials_sha256 == spec.project_credentials_sha256
    assert launched.load_project_credentials() == spec.load_project_credentials()
    (tmp_path / "project_credentials.md").write_text("changed")
    assert cli.main(args) != 0
    assert driver.await_count == 1
    assert cli.main(["-t", spec.target]) == 0
    assert driver.call_args.args[0].load_project_credentials() == ""


def test_cli_rejects_credentials_without_continuation_before_driver(tmp_path, monkeypatch):
    spec = credential_spec(tmp_path)
    driver = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, "_run_with_signal_handlers", driver)
    assert cli.main([
        "-t", spec.target, "--project-credentials-file", spec.project_credentials_file,
        "--project-credentials-sha256", spec.project_credentials_sha256,
    ]) != 0
    driver.assert_not_called()
