"""Artifact formats the platform's importers parse.

The finding-markdown parser keys on ``# <title>`` / ``**ID:**`` /
``**Severity:**`` within the first 15 lines; the CSV must carry CRLF
terminators and the exact column order; run.json is the status record.
"""

from __future__ import annotations

import csv
import io

import pytest

from strixops.platform import artifacts

REPORT = {
    "id": "vuln-0001",
    "title": "Reflected XSS in search",
    "severity": "medium",
    "timestamp": "2026-09-02 00:00:00 UTC",
    "target": "https://example.com",
    "description": "desc",
    "impact": "impact",
    "poc_description": "poc",
    "poc_script_code": "curl ...",
    "remediation_steps": "fix",
    "evidence": "evid",
    "cvss": 6.1,
}

INTERNAL = {
    "id": "int-0001",
    "finding_type": "credential",
    "title": "Local admin hash",
    "content": "admin:1000:aad3b…",
    "host": "10.10.10.5",
    "timestamp": "2026-09-02 00:00:00 UTC",
    "severity": "high",
}


@pytest.fixture()
def run_dir(tmp_path):
    artifacts.write_vulnerabilities(tmp_path, [dict(REPORT)])
    artifacts.write_internal_findings(tmp_path, [dict(INTERNAL)])
    artifacts.write_executive_report(tmp_path, "## Executive Summary\n\nbody")
    return tmp_path


def test_vuln_md_headers_within_first_15_lines(run_dir):
    lines = (run_dir / "vulnerabilities" / "vuln-0001.md").read_text(encoding="utf-8").splitlines()
    head = "\n".join(lines[:15])
    assert lines[0].startswith("# ")
    assert "**ID:**" in head
    assert "**Severity:**" in head
    assert "**Severity:** MEDIUM" in head


def test_csv_format(run_dir):
    raw = (run_dir / "vulnerabilities.csv").read_bytes()
    assert b"\r\n" in raw
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
    assert rows[0] == ["id", "title", "severity", "timestamp", "file"]
    assert rows[1][0] == "vuln-0001"
    assert rows[1][2] == "MEDIUM"
    assert rows[1][4] == "vulnerabilities/vuln-0001.md"


def test_csv_formula_injection_guard():
    assert artifacts.csv_safe("=cmd").startswith("'")
    assert artifacts.csv_safe("+1").startswith("'")
    assert artifacts.csv_safe("-2").startswith("'")
    assert artifacts.csv_safe("@x").startswith("'")
    assert artifacts.csv_safe("normal value") == "normal value"


def test_internal_finding_md_headers(run_dir):
    lines = (run_dir / "internal_findings" / "int-0001.md").read_text(encoding="utf-8").splitlines()
    head = "\n".join(lines[:15])
    assert lines[0].startswith("# ")
    assert "**ID:**" in head
    assert "**Severity:**" in head
    assert "## Details" in "\n".join(lines)


def test_severity_sort_order(tmp_path):
    reports = [
        {"id": "vuln-0003", "title": "t", "severity": "low", "timestamp": "2026-01-01 00:00:00 UTC"},
        {"id": "vuln-0001", "title": "t", "severity": "critical", "timestamp": "2026-01-02 00:00:00 UTC"},
        {"id": "vuln-0002", "title": "t", "severity": "high", "timestamp": "2026-01-01 00:00:00 UTC"},
    ]
    artifacts.write_vulnerabilities(tmp_path, reports)
    raw = (tmp_path / "vulnerabilities.csv").read_text(encoding="utf-8")
    rows = list(csv.reader(io.StringIO(raw)))
    assert [r[0] for r in rows[1:]] == ["vuln-0001", "vuln-0002", "vuln-0003"]


def test_run_record_roundtrip(tmp_path):
    record = {"run_name": "x_abcd", "status": "running"}
    artifacts.write_run_record(tmp_path, record)
    assert artifacts.read_run_record(tmp_path) == record
