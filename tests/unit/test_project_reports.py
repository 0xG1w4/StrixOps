"""Versioned deterministic project report snapshots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strixops.console.project_reports import (
    calculate_staleness,
    generate_project_report,
    list_project_reports,
    project_report_status,
    read_project_report,
    source_snapshot,
)


def _write_run(
    root: Path,
    name: str,
    *,
    target: str,
    status: str = "completed",
    title: str = "SQL Injection",
    severity: str = "high",
    executive_summary: str = "A validated issue was confirmed.",
    with_report: bool = True,
) -> Path:
    run_dir = root / name
    run_dir.mkdir()
    record = {
        "status": status,
        "start_time": f"2026-09-0{name[-1]}T10:00:00Z" if name[-1].isdigit() else "",
        "end_time": "2026-09-05T11:00:00Z",
        "duration_seconds": 3600,
        "scan_config": {
            "target": target,
            "scan_type": "web",
            "project_id": "prj_acme",
        },
        "scan_results": {
            "success": True,
            "executive_summary": executive_summary,
            "recommendations": "Use parameterized queries.",
        },
    }
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    if with_report:
        (run_dir / "penetration_test_report.md").write_text(
            f"# Task report {name}\n\n## Executive Summary\n\n{executive_summary}\n",
            encoding="utf-8",
        )
    (run_dir / "vulnerabilities.json").write_text(
        json.dumps(
            [
                {
                    "id": "vuln-0001",
                    "title": title,
                    "severity": severity,
                    "target": target,
                    "endpoint": "/search",
                    "cwe": "CWE-89",
                    "remediation_steps": "Use parameterized queries and validate input.",
                }
            ]
        ),
        encoding="utf-8",
    )
    return run_dir


@pytest.fixture
def project() -> dict:
    return {
        "id": "prj_acme",
        "name": "Acme External",
        "description": "Public application assessment",
        "scope": {
            "mode": "allowlist",
            "entries": [{"kind": "domain", "value": "example.test"}],
        },
    }


def test_generate_versioned_deterministic_zh_report(tmp_path: Path, project: dict) -> None:
    runs_root = tmp_path / "runs"
    store = tmp_path / "reports"
    runs_root.mkdir()
    run_one = _write_run(
        runs_root,
        "run1",
        target="https://example.test",
        severity="high",
        executive_summary="已确认注入漏洞。",
    )
    run_two = _write_run(
        runs_root,
        "run2",
        target="https://example.test",
        severity="critical",
        executive_summary="重复验证同一问题。",
    )
    running = _write_run(
        runs_root,
        "run3",
        target="https://staging.example.test",
        status="running",
    )

    first = generate_project_report(
        project,
        [run_two, run_one, running],
        language="zh-CN",
        storage_root=store,
    )
    second = generate_project_report(
        project,
        [run_one, running, run_two],
        language="zh",
        storage_root=store,
    )

    assert first["version_id"] == "v0001"
    assert second["version_id"] == "v0002"
    assert first["ready"] is True
    assert first["included_run_count"] == 2
    assert first["total_run_count"] == 3
    assert first["excluded_runs"] == [{"run": "run3", "reason": "status_running"}]
    assert first["stats"]["unique_vulnerability_count"] == 1
    assert first["stats"]["vulnerability_occurrence_count"] == 2
    assert first["stats"]["highest_severity"] == "critical"
    assert first["snapshot_summary"] == second["snapshot_summary"]
    finding = first["snapshot_summary"]["key_findings"][0]
    assert finding["severity"] == "critical"
    assert finding["source_runs"] == ["run1", "run2"]
    assert finding["occurrences"] == 2
    assert finding["title"] == "SQL Injection"

    first_read = read_project_report("prj_acme", 1, storage_root=store)
    second_read = read_project_report("prj_acme", "v0002", storage_root=store)
    assert first_read is not None and second_read is not None
    assert first_read["content"] == second_read["content"]
    assert "# 项目综合报告 — Acme External" in first_read["content"]
    assert "共记录 2 次已验证漏洞，去重后为 1 项" in first_read["content"]
    assert "已确认注入漏洞" in first_read["content"]
    assert "报告产生时间" not in first_read["content"]
    assert first_read["integrity_ok"] is True
    assert not list((store / "prj_acme").glob(".tmp-report-*"))

    versions = list_project_reports("prj_acme", storage_root=store)
    assert [item["version"] for item in versions] == [2, 1]


def test_staleness_detects_changed_new_and_project_inputs(tmp_path: Path, project: dict) -> None:
    runs_root = tmp_path / "runs"
    store = tmp_path / "reports"
    runs_root.mkdir()
    run_one = _write_run(runs_root, "run1", target="https://example.test")
    metadata = generate_project_report(project, [run_one], storage_root=store)

    unchanged = calculate_staleness(metadata, [run_one], project=project)
    assert unchanged["stale"] is False

    report_path = run_one / "penetration_test_report.md"
    report_path.write_text(report_path.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")
    changed = calculate_staleness(metadata, [run_one], project=project)
    assert changed["stale"] is True
    assert changed["stale_reasons"] == [{"code": "source_run_changed", "runs": ["run1"]}]

    run_two = _write_run(runs_root, "run2", target="https://api.example.test")
    changed_project = {**project, "description": "Updated description"}
    stale = calculate_staleness(metadata, [run_one, run_two], project=changed_project)
    codes = {reason["code"] for reason in stale["stale_reasons"]}
    assert codes == {"source_run_changed", "new_source_run", "project_changed"}

    status = project_report_status(
        "prj_acme",
        [run_one, run_two],
        project=changed_project,
        storage_root=store,
    )
    assert status["status"] == "outdated"
    assert status["version_count"] == 1


def test_english_report_and_corruption_status(tmp_path: Path, project: dict) -> None:
    runs_root = tmp_path / "runs"
    store = tmp_path / "reports"
    runs_root.mkdir()
    run_one = _write_run(runs_root, "run1", target="https://example.test")

    generated = generate_project_report(project, [run_one], language="en-US", storage_root=store)
    assert generated["language"] == "en"
    report = read_project_report("prj_acme", storage_root=store)
    assert report is not None
    assert "# Consolidated Project Report — Acme External" in report["content"]
    assert "## Remediation Priorities" in report["content"]

    artifact = store / "prj_acme" / "v0001" / "report.md"
    artifact.write_text("tampered", encoding="utf-8")
    corrupt = read_project_report("prj_acme", storage_root=store)
    assert corrupt is not None
    assert corrupt["integrity_ok"] is False
    assert corrupt["ready"] is False
    assert corrupt["status"] == "corrupt"
    status = project_report_status("prj_acme", [run_one], project=project, storage_root=store)
    assert status["status"] == "corrupt"


def test_source_snapshot_and_generation_validation(tmp_path: Path, project: dict) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    failed = _write_run(runs_root, "run1", target="x", status="failed", with_report=False)

    snapshot = source_snapshot([failed])
    assert snapshot["included_run_count"] == 0
    assert snapshot["excluded_runs"] == [{"run": "run1", "reason": "status_failed"}]

    with pytest.raises(ValueError, match="no completed runs"):
        generate_project_report(project, [failed], storage_root=tmp_path / "reports")
    with pytest.raises(ValueError, match="language"):
        generate_project_report(project, [failed], language="fr", storage_root=tmp_path / "reports")
    with pytest.raises(ValueError, match="project id"):
        generate_project_report({**project, "id": "../escape"}, [failed], storage_root=tmp_path)


def test_reader_summary_is_bounded_and_immutable(tmp_path: Path, project: dict) -> None:
    runs_root = tmp_path / "runs"
    store = tmp_path / "reports"
    runs_root.mkdir()
    run = _write_run(runs_root, "run1", target="https://example.test")
    findings_path = run / "vulnerabilities.json"
    findings_path.write_text(
        json.dumps(
            [
                {
                    "id": f"vuln-{index}",
                    "title": f"Finding {index:02d}",
                    "severity": "critical" if index == 9 else "low",
                    "endpoint": f"/{index}",
                }
                for index in range(10)
            ]
        ),
        encoding="utf-8",
    )
    generated = generate_project_report(project, [run], storage_root=store)
    summary = generated["snapshot_summary"]
    assert len(summary["key_findings"]) == 8
    assert summary["key_findings"][0]["title"] == "Finding 09"
    assert summary["key_findings"][0]["source_runs"] == ["run1"]
    assert generated["stats"]["unique_vulnerability_count"] == 10

    metadata_path = store / "prj_acme" / "v0001" / "metadata.json"
    original_metadata = metadata_path.read_bytes()
    findings_path.write_text("[]", encoding="utf-8")
    loaded = read_project_report("prj_acme", "v0001", run_dirs=[run], storage_root=store)
    assert loaded is not None
    assert loaded["stale"] is True
    assert loaded["snapshot_summary"] == summary
    assert loaded["stats"]["unique_vulnerability_count"] == 10
    assert metadata_path.read_bytes() == original_metadata


def test_legacy_reader_metadata_is_not_backfilled_on_read(tmp_path: Path, project: dict) -> None:
    runs_root = tmp_path / "runs"
    store = tmp_path / "reports"
    runs_root.mkdir()
    run = _write_run(runs_root, "run1", target="https://example.test")
    generate_project_report(project, [run], storage_root=store)
    metadata_path = store / "prj_acme" / "v0001" / "metadata.json"
    legacy = json.loads(metadata_path.read_text(encoding="utf-8"))
    del legacy["snapshot_summary"]
    metadata_path.write_text(json.dumps(legacy), encoding="utf-8")
    original_metadata = metadata_path.read_bytes()

    loaded = read_project_report("prj_acme", "v0001", run_dirs=[run], storage_root=store)
    listed = list_project_reports("prj_acme", run_dirs=[run], storage_root=store)
    assert loaded is not None and loaded["integrity_ok"] is True
    assert "snapshot_summary" not in loaded
    assert "snapshot_summary" not in listed[0]
    assert loaded["stats"]["unique_vulnerability_count"] == 1
    assert metadata_path.read_bytes() == original_metadata
