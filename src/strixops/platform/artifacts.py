"""Run artifacts on disk — formats the platform's importers expect.

Pinned formats (verified against the reference writer and the platform's
readers ``apps/api/services/{strix_data,pentest_flow}.py``):

* ``penetration_test_report.md`` — ``# Security Penetration Test Report``
  header, ``**Generated:**`` line, then the final report body.
* ``vulnerabilities.csv`` — columns ``id,title,severity,timestamp,file``,
  CRLF line terminators, severity uppercased, apostrophe-guarded cells,
  sorted by severity then timestamp.
* ``vulnerabilities/vuln-NNNN.md`` — header block ``# <title>`` /
  ``**ID:**`` / ``**Severity:**`` / ``**Found:**`` within the first lines
  (the platform's finding parser keys on exactly these).
* ``internal_findings/int-NNNN.md`` — same header discipline, ordered
  credential → result → sensitive → architecture.
* ``run.json`` — the run record, pretty-printed.

All writes are atomic (sibling temp file + rename) with ``newline=""`` so
the CSV's own ``\\r\\n`` terminators land byte-for-byte.
"""

from __future__ import annotations

import csv
import io
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

INTERNAL_FINDING_TYPE_ORDER = {"credential": 0, "result": 1, "sensitive": 2, "architecture": 3}

CSV_FIELDNAMES = ["id", "title", "severity", "timestamp", "file"]


def utc_stamp() -> str:
    """Finding timestamps: ``YYYY-MM-DD HH:MM:SS UTC``."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def atomic_write_text(path: Path, payload: str) -> None:
    """Write *payload* to *path* via sibling temp file + atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def csv_safe(value: object) -> str:
    """Apostrophe-prefix cells that could execute as formulas when opened.

    Guards a leading ``= + - @``, tab, or carriage return; the csv module's
    own quoting handles the rest.
    """
    text = str(value if value is not None else "")
    first = text[:1]
    if first in {"=", "+", "-", "@"} or "\t" in text or "\r" in text:
        return f"'{text}"
    return text


# ---------------------------------------------------------------- run record


def write_run_record(run_dir: Path, run_record: dict[str, Any]) -> None:
    atomic_write_text(
        Path(run_dir) / "run.json",
        json.dumps(run_record, ensure_ascii=False, indent=2, default=str),
    )


def read_run_record(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir) / "run.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------- executive md


def write_executive_report(run_dir: Path, final_scan_result: str) -> None:
    """Write the executive report verbatim — the composer owns the full
    platform-format header (title/target/type/time/severity block)."""
    path = Path(run_dir) / "penetration_test_report.md"
    atomic_write_text(path, final_scan_result)


# ----------------------------------------------------------- vulnerabilities


def write_vulnerabilities(run_dir: Path, reports: list[dict[str, Any]]) -> None:
    """Write the per-finding markdown, the CSV index, and the JSON list."""
    run_dir = Path(run_dir)
    vuln_dir = run_dir / "vulnerabilities"
    vuln_dir.mkdir(parents=True, exist_ok=True)

    for report in reports:
        atomic_write_text(vuln_dir / f"{report['id']}.md", render_vulnerability_md(report))

    sorted_reports = sorted(
        reports,
        key=lambda r: (SEVERITY_ORDER.get(str(r.get("severity", "info")).lower(), 5), r.get("timestamp", "")),
    )

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDNAMES, lineterminator="\r\n")
    writer.writeheader()
    for report in sorted_reports:
        writer.writerow(
            {
                "id": csv_safe(report["id"]),
                "title": csv_safe(report.get("title", "")),
                "severity": csv_safe(str(report.get("severity", "info")).upper()),
                "timestamp": csv_safe(report.get("timestamp", "")),
                "file": csv_safe(f"vulnerabilities/{report['id']}.md"),
            }
        )
    atomic_write_text(run_dir / "vulnerabilities.csv", buf.getvalue())

    atomic_write_text(
        run_dir / "vulnerabilities.json",
        json.dumps(reports, ensure_ascii=False, indent=2, default=str),
    )


def render_vulnerability_md(report: dict[str, Any]) -> str:
    lines: list[str] = [
        f"# {report.get('title', 'Untitled Vulnerability')}\n",
        f"**ID:** {report.get('id', 'unknown')}",
        f"**Severity:** {str(report.get('severity', 'unknown')).upper()}",
        f"**Found:** {report.get('timestamp', 'unknown')}",
    ]
    if report.get("target"):
        lines.append(f"\n**Target:** {report['target']}")
    if report.get("endpoint"):
        lines.append(
            f"**Endpoint:** {report['endpoint']}" + (f" ({report['method']})" if report.get("method") else "")
        )
    for key, label in (
        ("finding_class", "Finding Class"),
        ("confidence", "Confidence"),
        ("fix_effort", "Fix Effort"),
    ):
        if report.get(key):
            lines.append(f"**{label}:** {report[key]}")

    _section(lines, "Description", report.get("description"))
    _section(lines, "Impact", report.get("impact"))
    _section(lines, "Counterevidence", report.get("counterevidence"))
    _section(lines, "Confidence Rationale", report.get("confidence_rationale"))
    _section(lines, "What Would Change This Severity", report.get("severity_change_conditions"))
    _section(lines, "Technical Analysis", report.get("technical_analysis"))
    _section(lines, "Remediation", report.get("remediation_steps"))
    _section(lines, "Evidence", report.get("evidence"))
    _section(lines, "Assumptions", report.get("assumptions"))
    _section(lines, "Dependency Metadata", report.get("dependency_metadata"))
    _section(lines, "Code Locations and Fixes", report.get("code_locations"))
    _section(lines, "Fix Verification", report.get("fix_verification"))
    _section(lines, "Suggested Pull Request", report.get("fix_pr_body"))

    cvss = report.get("cvss")
    if cvss is not None:
        lines.append("\n## Contextual CVSS\n")
        lines.append(f"**CVSS:** {cvss}")
        if report.get("cvss_vector"):
            lines.append(f"**Vector:** {report['cvss_vector']}")
        _section(lines, "CVSS Metrics", report.get("cvss_breakdown"))
    if report.get("poc_description") or report.get("poc_script_code"):
        lines.append("\n## Proof of Concept\n")
        if report.get("poc_description"):
            lines.append(f"{report['poc_description']}\n")
        if report.get("poc_script_code"):
            lines.append(_code_block(str(report["poc_script_code"])))
    _section(lines, "Update History", report.get("update_history"))
    return "\n".join(lines).rstrip() + "\n"


def _code_block(value: str, language: str = "") -> str:
    # Preserve arbitrary evidence/code containing its own Markdown fences.
    fence = "```"
    while fence in value:
        fence += "`"
    return f"{fence}{language}\n{value}\n{fence}"


def _section(lines: list[str], title: str, value: Any) -> None:
    if value in (None, "", []):
        return
    lines.append(f"\n## {title}\n")
    if isinstance(value, (dict, list)):
        lines.append(_code_block(json.dumps(value, ensure_ascii=False, indent=2, default=str), "json"))
    else:
        lines.append(str(value))


# -------------------------------------------------------- internal findings


def write_internal_findings(run_dir: Path, findings: list[dict[str, Any]]) -> None:
    out_dir = Path(run_dir) / "internal_findings"
    out_dir.mkdir(exist_ok=True)
    ordered = sorted(
        findings,
        key=lambda f: (
            INTERNAL_FINDING_TYPE_ORDER.get(str(f.get("finding_type", "result")), 9),
            f.get("timestamp", ""),
        ),
    )
    for finding in ordered:
        atomic_write_text(out_dir / f"{finding['id']}.md", render_internal_finding_md(finding))


def render_internal_finding_md(finding: dict[str, Any]) -> str:
    lines: list[str] = [
        f"# {finding.get('title', 'Untitled Finding')}\n",
        f"**ID:** {finding.get('id', 'unknown')}",
        f"**Type:** {finding.get('finding_type', 'result')}",
        f"**Found:** {finding.get('timestamp', 'unknown')}",
    ]
    if finding.get("host"):
        lines.append(f"**Host:** {finding['host']}")
    if finding.get("source"):
        lines.append(f"**Source:** {finding['source']}")
    if finding.get("severity"):
        lines.append(f"**Severity:** {str(finding['severity']).upper()}")
    lines.append("\n## Details\n")
    lines.append(str(finding.get("content", "")))
    metadata = finding.get("metadata")
    if metadata:
        lines.append("\n## Metadata\n")
        lines.append("```json")
        lines.append(json.dumps(metadata, ensure_ascii=False, indent=2, default=str))
        lines.append("```\n")
    return "\n".join(lines).rstrip() + "\n"
