"""Durable, deterministic project-report artifacts.

Project reports are snapshots, not live views.  Every generated version keeps
the exact set and hashes of the completed task reports that were included.
The Markdown renderer is deliberately deterministic and does not call an LLM;
generation time and version number live in metadata rather than the artifact
body, so equal inputs produce byte-identical Markdown.

The default store is ``~/.strixops/project_reports`` and can be overridden by
``STRIXOPS_PROJECT_REPORTS_DIR`` or an explicit ``storage_root`` argument.
Each version is published with an atomic directory rename::

    <root>/<project_id>/v0001/metadata.json
    <root>/<project_id>/v0001/report.md
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strixops.console import parser

SCHEMA_VERSION = 1
TASK_REPORT_FILENAME = "penetration_test_report.md"
ARTIFACT_FILENAME = "report.md"
METADATA_FILENAME = "metadata.json"

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION_RE = re.compile(r"^v([0-9]{4,})$")
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SUMMARY_FINDINGS_LIMIT = 8


def reports_root(storage_root: Path | None = None) -> Path:
    """Resolve the project-report store without creating it."""
    if storage_root is not None:
        return Path(storage_root).expanduser()
    override = (os.environ.get("STRIXOPS_PROJECT_REPORTS_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".strixops" / "project_reports"


def normalize_language(language: str) -> str:
    """Normalize supported report locales to ``zh-CN`` or ``en``."""
    value = str(language or "").strip().lower().replace("_", "-")
    if value in {"zh", "zh-cn", "zh-hans"}:
        return "zh-CN"
    if value in {"en", "en-us", "en-gb"}:
        return "en"
    raise ValueError("language must be zh-CN or en")


def _safe_project_id(project_id: str) -> str:
    value = str(project_id or "").strip()
    if not _PROJECT_ID_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError("invalid project id")
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _unique_run_dirs(run_dirs: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw in run_dirs:
        path = Path(raw)
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path.absolute())
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _project_snapshot(project: Mapping[str, Any]) -> dict[str, Any]:
    """Fields that influence rendered content or describe its scope."""
    snapshot = {
        "id": str(project.get("id") or ""),
        "name": str(project.get("name") or ""),
        "description": str(project.get("description") or ""),
    }
    # Support either name while project scope storage evolves.
    if "scope" in project:
        snapshot["scope"] = _json_safe(project.get("scope"))
    if "scope_rules" in project:
        snapshot["scope_rules"] = _json_safe(project.get("scope_rules"))
    return snapshot


def _record_snapshot(record: Mapping[str, Any]) -> dict[str, Any]:
    scan_config = record.get("scan_config")
    if not isinstance(scan_config, Mapping):
        scan_config = {}
    scan_results = record.get("scan_results")
    if not isinstance(scan_results, Mapping):
        scan_results = {}
    targets = parser.scan_targets(dict(scan_config))
    scope = {
        "target": str(scan_config.get("target") or ""),
        "scan_type": str(scan_config.get("scan_type") or ""),
        "project_id": str(scan_config.get("project_id") or ""),
    }
    # Retain legacy single-run hashes; a multi-run snapshot binds every target.
    if "targets" in scan_config:
        scope.update(targets=targets, target_count=len(targets))
    return {
        "status": str(record.get("status") or "unknown"),
        "start_time": str(record.get("start_time") or ""),
        "end_time": str(record.get("end_time") or ""),
        "duration_seconds": int(record.get("duration_seconds") or 0),
        "scan_config": scope,
        "scan_results": _json_safe(dict(scan_results)),
    }


def _collect_sources(
    run_dirs: Iterable[Path],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    """Collect completed runs with readable final reports and exclusion reasons."""
    sources: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    dirs = _unique_run_dirs(run_dirs)

    for run_dir in dirs:
        record = parser.json_load(run_dir / "run.json")
        status = str(record.get("status") or "unknown").strip().lower()
        report_bytes = _read_bytes(run_dir / TASK_REPORT_FILENAME)
        if status != "completed":
            excluded.append({"run": run_dir.name, "reason": f"status_{status or 'unknown'}"})
            continue
        if report_bytes is None:
            excluded.append({"run": run_dir.name, "reason": "missing_final_report"})
            continue

        try:
            record_input = _record_snapshot(record)
        except ValueError:
            excluded.append({"run": run_dir.name, "reason": "invalid_target_scope"})
            continue
        findings = parser.parse_findings(run_dir)
        input_value = {
            "run": run_dir.name,
            "record": record_input,
            "report_sha256": _sha256(report_bytes),
            "findings": findings,
        }
        vulnerabilities = findings.get("vulnerabilities")
        internal = findings.get("internal")
        if not isinstance(vulnerabilities, list):
            vulnerabilities = []
        if not isinstance(internal, list):
            internal = []

        source = {
            "run": run_dir.name,
            "status": "completed",
            "target": str(record_input["scan_config"].get("target") or ""),
            "scan_type": str(record_input["scan_config"].get("scan_type") or ""),
            "start_time": str(record_input.get("start_time") or ""),
            "end_time": str(record_input.get("end_time") or ""),
            "report_sha256": _sha256(report_bytes),
            "source_sha256": _canonical_hash(input_value),
            "vulnerability_count": len(vulnerabilities),
            "internal_finding_count": len(internal),
            # Private render inputs are stripped before metadata persistence.
            "_record": record_input,
            "_report": report_bytes.decode("utf-8", errors="replace"),
            "_vulnerabilities": vulnerabilities,
            "_internal": internal,
        }
        if "targets" in record_input["scan_config"]:
            source["targets"] = record_input["scan_config"]["targets"]
            source["target_count"] = record_input["scan_config"]["target_count"]
        sources.append(source)

    sources.sort(key=lambda item: (str(item["start_time"]), str(item["run"])))
    excluded.sort(key=lambda item: item["run"])
    return sources, excluded, len(dirs)


def _public_source(source: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in source.items() if not str(key).startswith("_")}


def source_snapshot(run_dirs: Iterable[Path]) -> dict[str, Any]:
    """Return the current eligible source snapshot without generating a report."""
    sources, excluded, total = _collect_sources(run_dirs)
    public_sources = [_public_source(source) for source in sources]
    return {
        "source_runs": public_sources,
        "source_snapshot_hash": _canonical_hash(public_sources),
        "included_run_count": len(public_sources),
        "excluded_runs": excluded,
        "total_run_count": total,
    }


def _inline(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", "").split()) or "—"


def _source_target_label(source: Mapping[str, Any]) -> str:
    return ", ".join(_inline(target) for target in parser.scan_targets(dict(source))) or "—"


def _table(value: Any) -> str:
    return _inline(value).replace("|", "\\|")


def _severity(value: Any) -> str:
    severity = str(value or "info").strip().lower()
    return severity if severity in _SEVERITY_ORDER else "info"


def _dependency_identity(finding: Mapping[str, Any]) -> dict[str, str]:
    metadata = finding.get("dependency_metadata")
    if not isinstance(metadata, Mapping):
        return {}
    return {
        key: str(metadata[key]).strip()
        for key in ("package_name", "package_ecosystem", "manifest_path")
        if metadata.get(key) and str(metadata[key]).strip()
    }


def _finding_class(finding: Mapping[str, Any]) -> str:
    declared = str(finding.get("finding_class") or "").strip().lower()
    if declared:
        return declared
    # Older dependency reports predate the explicit finding_class field.
    metadata = finding.get("dependency_metadata")
    return "dependency_cve" if isinstance(metadata, Mapping) and metadata else "dynamic"


def _finding_key(finding: Mapping[str, Any], default_target: str) -> str:
    target = _inline(finding.get("target") or default_target).lower()
    endpoint = _inline(finding.get("endpoint") or "").lower()
    cve = _inline(finding.get("cve") or "").lower()
    cwe = _inline(finding.get("cwe") or "").lower()
    title = _inline(finding.get("title") or finding.get("id") or "finding").lower()
    identity = cve if cve != "—" else f"{cwe}:{title}"
    dependency = _dependency_identity(finding)
    return _canonical_hash(
        [
            target,
            endpoint,
            identity,
            _finding_class(finding),
            str(finding.get("method") or "").strip().upper(),
            dependency.get("package_name", "").lower(),
            dependency.get("package_ecosystem", "").lower(),
            # Repository paths can differ only by case. Missing identity fields
            # stay unknown rather than acting as wildcards for distinct findings.
            dependency.get("manifest_path", ""),
        ]
    )[:16]


def _deduplicated_vulnerabilities(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for source in sources:
        targets = parser.scan_targets(source)
        default_target = targets[0] if len(targets) == 1 else ""
        for finding in source["_vulnerabilities"]:
            if not isinstance(finding, Mapping):
                continue
            # An unlocated finding in a multi-target run cannot be attributed to
            # the primary or merged across targets merely because its title matches.
            identity_target = default_target
            if len(targets) > 1 and not finding.get("target"):
                identity_target = f"unknown:{source['run']}:{finding.get('id', '')}"
            key = _finding_key(finding, identity_target)
            severity = _severity(finding.get("severity"))
            current = grouped.get(key)
            if current is None:
                current = {
                    "fingerprint": key,
                    "title": _inline(finding.get("title") or finding.get("id") or "Finding"),
                    "severity": severity,
                    "target": _inline(finding.get("target") or default_target),
                    "endpoint": _inline(finding.get("endpoint") or ""),
                    "method": str(finding.get("method") or "").strip().upper(),
                    "finding_class": _finding_class(finding),
                    "dependency_metadata": _dependency_identity(finding),
                    "cve": _inline(finding.get("cve") or ""),
                    "cwe": _inline(finding.get("cwe") or ""),
                    "remediation": str(finding.get("remediation_steps") or "").strip(),
                    "source_runs": [],
                    "occurrences": 0,
                }
                grouped[key] = current
            if _SEVERITY_ORDER[severity] < _SEVERITY_ORDER[str(current["severity"])]:
                current["severity"] = severity
            run_name = str(source["run"])
            if run_name not in current["source_runs"]:
                current["source_runs"].append(run_name)
            current["occurrences"] += 1

    rows = list(grouped.values())
    for row in rows:
        row["source_runs"].sort()
    rows.sort(
        key=lambda item: (
            _SEVERITY_ORDER[str(item["severity"])],
            str(item["title"]).lower(),
            str(item["target"]).lower(),
        )
    )
    return rows


def _deduplicated_internal(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for source in sources:
        targets = parser.scan_targets(source)
        default_target = targets[0] if len(targets) == 1 else ""
        for finding in source["_internal"]:
            if not isinstance(finding, Mapping):
                continue
            value = {
                "finding_type": _inline(finding.get("finding_type") or "result"),
                "title": _inline(finding.get("title") or finding.get("id") or "Finding"),
                "host": _inline(finding.get("host") or default_target),
                "severity": _severity(finding.get("severity")),
            }
            identity = [value["finding_type"].lower(), value["title"].lower(), value["host"].lower()]
            if len(targets) > 1 and not finding.get("host"):
                identity.extend([str(source["run"]), str(finding.get("id") or "")])
            key = _canonical_hash(identity)[:16]
            current = grouped.get(key)
            if current is None:
                current = {**value, "fingerprint": key, "source_runs": [], "occurrences": 0}
                grouped[key] = current
            run_name = str(source["run"])
            if run_name not in current["source_runs"]:
                current["source_runs"].append(run_name)
            current["occurrences"] += 1
    rows = list(grouped.values())
    for row in rows:
        row["source_runs"].sort()
    rows.sort(key=lambda item: (str(item["finding_type"]), str(item["title"]), str(item["host"])))
    return rows


def _extract_section(markdown: str, headings: set[str]) -> str:
    lines = markdown.splitlines()
    capture = False
    output: list[str] = []
    normalized = {heading.casefold() for heading in headings}
    for line in lines:
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            heading = match.group(1).strip().casefold()
            if capture:
                break
            capture = heading in normalized
            continue
        if capture:
            output.append(line)
    return "\n".join(output).strip()


def _source_summary(source: Mapping[str, Any]) -> str:
    record = source.get("_record")
    if isinstance(record, Mapping):
        results = record.get("scan_results")
        if isinstance(results, Mapping):
            summary = str(results.get("executive_summary") or "").strip()
            if summary:
                return summary
    return _extract_section(
        str(source.get("_report") or ""),
        {"执行摘要", "執行摘要", "Executive Summary"},
    )


def _blockquote(value: str) -> list[str]:
    lines = value.strip().splitlines()
    return [f"> {line}" if line else ">" for line in lines]


def _scope_label(project: Mapping[str, Any]) -> str:
    scope = project.get("scope") if "scope" in project else project.get("scope_rules")
    if scope in (None, "", [], {}):
        return "*"
    if isinstance(scope, str):
        return _inline(scope)
    if isinstance(scope, list):
        values = []
        for entry in scope:
            if isinstance(entry, Mapping):
                values.append(_inline(entry.get("value") or ""))
            else:
                values.append(_inline(entry))
        return ", ".join(value for value in values if value != "—") or "*"
    if isinstance(scope, Mapping):
        mode = str(scope.get("mode") or "").lower()
        if mode == "unrestricted":
            return "*"
        entries = scope.get("entries") or scope.get("rules")
        if isinstance(entries, list):
            values = []
            for entry in entries:
                if isinstance(entry, Mapping):
                    values.append(_inline(entry.get("value") or ""))
                else:
                    values.append(_inline(entry))
            return ", ".join(value for value in values if value != "—") or "*"
    return _inline(scope)


def _short_remediation(value: str, limit: int = 360) -> str:
    compact = _inline(value)
    if compact == "—" or len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _finding_location(finding: Mapping[str, Any]) -> str:
    parts = [str(finding["target"])]
    if finding["method"]:
        parts.append(str(finding["method"]))
    if finding["endpoint"] != "—":
        parts.append(str(finding["endpoint"]))
    dependency = finding["dependency_metadata"]
    package = "/".join(
        dependency[key] for key in ("package_ecosystem", "package_name") if dependency.get(key)
    )
    if package:
        parts.append(f"· {package}")
    if dependency.get("manifest_path"):
        parts.append(f"· {dependency['manifest_path']}")
    return " ".join(parts)


def _render_report(
    project: Mapping[str, Any],
    sources: list[dict[str, Any]],
    *,
    language: str,
    snapshot_hash: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    findings = _deduplicated_vulnerabilities(sources)
    internal = _deduplicated_internal(sources)
    severity_counts = {severity: 0 for severity in _SEVERITY_ORDER}
    for finding in findings:
        severity_counts[str(finding["severity"])] += 1
    occurrence_count = sum(int(finding["occurrences"]) for finding in findings)
    targets = sorted({target for source in sources for target in parser.scan_targets(source)})
    highest = next((severity for severity in _SEVERITY_ORDER if severity_counts[severity]), "info")
    name = _inline(project.get("name") or project.get("id") or "Project")
    description = _inline(project.get("description") or "")
    scope = _scope_label(project)

    if language == "zh-CN":
        lines = [
            f"# 项目综合报告 — {name}",
            "",
            f"- **项目：** {name}",
            f"- **描述：** {description}",
            f"- **目标范围：** {_inline(scope)}",
            f"- **数据快照：** `{snapshot_hash[:16]}`",
            "",
            "## 执行摘要",
            "",
            f"本报告汇总 {len(sources)} 个已完成任务的最终报告。"
            f"共记录 {occurrence_count} 次已验证漏洞，去重后为 {len(findings)} 项；"
            f"项目当前最高风险等级为 **{highest.upper()}**。",
            "",
            "## 范围与覆盖",
            "",
            f"- 已纳入任务：{len(sources)}",
            f"- 唯一目标：{len(targets)}",
            f"- 项目范围：{_inline(scope)}",
            f"- 目标：{', '.join(targets)}",
            "",
            "## 风险概览",
            "",
            "| 等级 | 去重漏洞 |",
            "|---|---:|",
        ]
        lines += [f"| {severity.upper()} | {severity_counts[severity]} |" for severity in _SEVERITY_ORDER]
        lines += ["", "## 去重漏洞", ""]
        if findings:
            lines += [
                "| 等级 | 漏洞 | 类型 | 目标 / 端点 / 依赖 | 出现次数 | 来源任务 |",
                "|---|---|---|---|---:|---|",
            ]
            for finding in findings:
                finding_class = {
                    "dynamic": "动态验证",
                    "dependency_cve": "依赖 CVE",
                }.get(finding["finding_class"], finding["finding_class"])
                lines.append(
                    f"| {str(finding['severity']).upper()} | {_table(finding['title'])} | "
                    f"{_table(finding_class)} | {_table(_finding_location(finding))} | "
                    f"{finding['occurrences']} | "
                    f"{_table(', '.join(finding['source_runs']))} |"
                )
        else:
            lines.append("纳入的最终报告中没有已验证漏洞。")
        lines += ["", "## 内网发现", ""]
        if internal:
            lines += ["| 类型 | 发现 | 主机 | 出现次数 | 来源任务 |", "|---|---|---|---:|---|"]
            for finding in internal:
                lines.append(
                    f"| {_table(finding['finding_type'])} | {_table(finding['title'])} | "
                    f"{_table(finding['host'])} | {finding['occurrences']} | "
                    f"{_table(', '.join(finding['source_runs']))} |"
                )
        else:
            lines.append("纳入的最终报告中没有内网发现。")
        lines += ["", "## 任务摘要", ""]
        for source in sources:
            lines += [
                f"### {_source_target_label(source)} (`{_inline(source['run'])}`)",
                "",
                f"- 漏洞：{source['vulnerability_count']}",
                f"- 内网发现：{source['internal_finding_count']}",
            ]
            summary = _source_summary(source)
            if summary:
                lines += ["", *_blockquote(summary)]
            lines.append("")
        lines += ["## 修复优先级", ""]
        if findings:
            for finding in findings:
                remediation = _short_remediation(str(finding.get("remediation") or ""))
                lines.append(
                    f"- **[{str(finding['severity']).upper()}] {_inline(finding['title'])}：** {remediation}"
                )
        else:
            lines.append("当前没有基于已验证漏洞的修复项目。")
        lines += ["", "## 来源任务", ""]
        for source in sources:
            lines.append(
                f"- `{_inline(source['run'])}` · {_source_target_label(source)} · "
                f"report `{str(source['report_sha256'])[:12]}`"
            )
    else:
        lines = [
            f"# Consolidated Project Report — {name}",
            "",
            f"- **Project:** {name}",
            f"- **Description:** {description}",
            f"- **Target scope:** {_inline(scope)}",
            f"- **Data snapshot:** `{snapshot_hash[:16]}`",
            "",
            "## Executive Summary",
            "",
            f"This report consolidates the final reports from {len(sources)} completed tasks. "
            f"They contain {occurrence_count} validated vulnerability occurrences, "
            f"representing {len(findings)} unique findings. The current highest project risk "
            f"is **{highest.upper()}**.",
            "",
            "## Scope & Coverage",
            "",
            f"- Included tasks: {len(sources)}",
            f"- Unique targets: {len(targets)}",
            f"- Project scope: {_inline(scope)}",
            f"- Targets: {', '.join(targets)}",
            "",
            "## Risk Overview",
            "",
            "| Severity | Unique findings |",
            "|---|---:|",
        ]
        lines += [f"| {severity.upper()} | {severity_counts[severity]} |" for severity in _SEVERITY_ORDER]
        lines += ["", "## Deduplicated Findings", ""]
        if findings:
            lines += [
                "| Severity | Finding | Class | Target / endpoint / dependency | "
                "Occurrences | Source tasks |",
                "|---|---|---|---|---:|---|",
            ]
            for finding in findings:
                finding_class = {
                    "dynamic": "Dynamic",
                    "dependency_cve": "Dependency CVE",
                }.get(finding["finding_class"], finding["finding_class"])
                lines.append(
                    f"| {str(finding['severity']).upper()} | {_table(finding['title'])} | "
                    f"{_table(finding_class)} | {_table(_finding_location(finding))} | "
                    f"{finding['occurrences']} | "
                    f"{_table(', '.join(finding['source_runs']))} |"
                )
        else:
            lines.append("No validated vulnerabilities appear in the included final reports.")
        lines += ["", "## Internal Findings", ""]
        if internal:
            lines += [
                "| Type | Finding | Host | Occurrences | Source tasks |",
                "|---|---|---|---:|---|",
            ]
            for finding in internal:
                lines.append(
                    f"| {_table(finding['finding_type'])} | {_table(finding['title'])} | "
                    f"{_table(finding['host'])} | {finding['occurrences']} | "
                    f"{_table(', '.join(finding['source_runs']))} |"
                )
        else:
            lines.append("No internal findings appear in the included final reports.")
        lines += ["", "## Task Summaries", ""]
        for source in sources:
            lines += [
                f"### {_source_target_label(source)} (`{_inline(source['run'])}`)",
                "",
                f"- Vulnerabilities: {source['vulnerability_count']}",
                f"- Internal findings: {source['internal_finding_count']}",
            ]
            summary = _source_summary(source)
            if summary:
                lines += ["", *_blockquote(summary)]
            lines.append("")
        lines += ["## Remediation Priorities", ""]
        if findings:
            for finding in findings:
                remediation = _short_remediation(str(finding.get("remediation") or ""))
                lines.append(
                    f"- **[{str(finding['severity']).upper()}] {_inline(finding['title'])}:** {remediation}"
                )
        else:
            lines.append("There are no remediation items based on validated findings.")
        lines += ["", "## Source Tasks", ""]
        for source in sources:
            lines.append(
                f"- `{_inline(source['run'])}` · {_source_target_label(source)} · "
                f"report `{str(source['report_sha256'])[:12]}`"
            )

    stats = {
        "unique_vulnerability_count": len(findings),
        "vulnerability_occurrence_count": occurrence_count,
        "unique_internal_finding_count": len(internal),
        "severity": severity_counts,
        "highest_severity": highest,
        "target_count": len(targets),
    }
    # Structured reader data is captured alongside the artifact, not reconstructed
    # from mutable run directories when an older version is opened. Keep only a
    # bounded preview; the complete findings remain in the Markdown artifact.
    snapshot_summary = {
        "key_findings": [
            {
                key: finding[key]
                for key in (
                    "fingerprint",
                    "title",
                    "severity",
                    "target",
                    "endpoint",
                    "method",
                    "finding_class",
                    "dependency_metadata",
                    "occurrences",
                    "source_runs",
                )
            }
            for finding in findings[:_SUMMARY_FINDINGS_LIMIT]
        ],
    }
    return "\n".join(lines).rstrip() + "\n", stats, snapshot_summary


@contextmanager
def _locked_project_dir(project_dir: Path) -> Iterator[None]:
    project_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(project_dir, 0o700)
    lock_path = project_dir / ".lock"
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_private(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _version_dirs(project_dir: Path) -> list[tuple[int, Path]]:
    try:
        children = list(project_dir.iterdir())
    except OSError:
        return []
    result: list[tuple[int, Path]] = []
    for path in children:
        match = _VERSION_RE.fullmatch(path.name)
        if match and path.is_dir():
            result.append((int(match.group(1)), path))
    result.sort(key=lambda item: item[0], reverse=True)
    return result


def generate_project_report(
    project: Mapping[str, Any],
    run_dirs: Iterable[Path],
    *,
    language: str = "zh-CN",
    storage_root: Path | None = None,
) -> dict[str, Any]:
    """Generate and atomically publish a new deterministic report version.

    Raises ``ValueError`` when the project id/language is invalid or no
    completed task has a readable final report.
    """
    project_id = _safe_project_id(str(project.get("id") or ""))
    locale = normalize_language(language)
    project_data = _project_snapshot(project)
    sources, excluded, total_run_count = _collect_sources(run_dirs)
    if not sources:
        raise ValueError("no completed runs with final reports")

    public_sources = [_public_source(source) for source in sources]
    snapshot_hash = _canonical_hash(public_sources)
    project_hash = _canonical_hash(project_data)
    markdown, stats, snapshot_summary = _render_report(
        project_data,
        sources,
        language=locale,
        snapshot_hash=snapshot_hash,
    )
    artifact_bytes = markdown.encode("utf-8")
    artifact_hash = _sha256(artifact_bytes)
    input_hash = _canonical_hash(
        {
            "schema_version": SCHEMA_VERSION,
            "language": locale,
            "project_snapshot_hash": project_hash,
            "source_snapshot_hash": snapshot_hash,
        }
    )

    root = reports_root(storage_root)
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)
    project_dir = root / project_id

    with _locked_project_dir(project_dir):
        existing = _version_dirs(project_dir)
        version = (existing[0][0] if existing else 0) + 1
        version_id = f"v{version:04d}"
        final_dir = project_dir / version_id
        temp_dir = Path(tempfile.mkdtemp(prefix=".tmp-report-", dir=project_dir))
        try:
            with contextlib.suppress(OSError):
                os.chmod(temp_dir, 0o700)
            metadata: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "project_id": project_id,
                "project_name": project_data["name"],
                "version": version,
                "version_id": version_id,
                "status": "ready",
                "ready": True,
                "language": locale,
                "generated_at": _utc_now_iso(),
                "artifact_filename": ARTIFACT_FILENAME,
                "artifact_sha256": artifact_hash,
                "generation_input_hash": input_hash,
                "project_snapshot": project_data,
                "project_snapshot_hash": project_hash,
                "source_runs": public_sources,
                "source_snapshot_hash": snapshot_hash,
                "included_run_count": len(public_sources),
                "excluded_runs": excluded,
                "total_run_count": total_run_count,
                "stats": stats,
                "snapshot_summary": snapshot_summary,
            }
            _write_private(temp_dir / ARTIFACT_FILENAME, artifact_bytes)
            _write_private(
                temp_dir / METADATA_FILENAME,
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            )
            # Publishing the directory makes report + metadata visible as one unit.
            os.replace(temp_dir, final_dir)
            with contextlib.suppress(OSError):
                directory_fd = os.open(project_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except Exception:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            raise

    return {**metadata, "stale": False, "stale_reasons": []}


def _load_version(version_dir: Path, *, include_content: bool) -> dict[str, Any] | None:
    try:
        metadata_value = json.loads((version_dir / METADATA_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata_value, dict):
        return None
    metadata = dict(metadata_value)
    artifact_name = str(metadata.get("artifact_filename") or ARTIFACT_FILENAME)
    if Path(artifact_name).name != artifact_name:
        return None
    artifact_bytes = _read_bytes(version_dir / artifact_name)
    expected_hash = str(metadata.get("artifact_sha256") or "")
    integrity_ok = artifact_bytes is not None and bool(expected_hash)
    if artifact_bytes is not None and expected_hash:
        integrity_ok = _sha256(artifact_bytes) == expected_hash
    metadata["integrity_ok"] = integrity_ok
    metadata["ready"] = metadata.get("status") == "ready" and integrity_ok
    if not integrity_ok:
        metadata["status"] = "corrupt"
    if include_content:
        metadata["content"] = (
            artifact_bytes.decode("utf-8", errors="replace") if artifact_bytes is not None else ""
        )
    return metadata


def calculate_staleness(
    metadata: Mapping[str, Any],
    run_dirs: Iterable[Path],
    *,
    project: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare a stored report snapshot with current eligible task reports."""
    current = source_snapshot(run_dirs)
    stored_sources_value = metadata.get("source_runs")
    stored_sources = stored_sources_value if isinstance(stored_sources_value, list) else []
    stored_by_run = {
        str(item.get("run")): str(item.get("source_sha256") or "")
        for item in stored_sources
        if isinstance(item, Mapping) and item.get("run")
    }
    current_by_run = {
        str(item.get("run")): str(item.get("source_sha256") or "")
        for item in current["source_runs"]
        if isinstance(item, Mapping) and item.get("run")
    }

    reasons: list[dict[str, Any]] = []
    new_runs = sorted(set(current_by_run) - set(stored_by_run))
    removed_runs = sorted(set(stored_by_run) - set(current_by_run))
    changed_runs = sorted(
        run for run in set(current_by_run) & set(stored_by_run) if current_by_run[run] != stored_by_run[run]
    )
    if new_runs:
        reasons.append({"code": "new_source_run", "runs": new_runs})
    if removed_runs:
        reasons.append({"code": "source_run_removed", "runs": removed_runs})
    if changed_runs:
        reasons.append({"code": "source_run_changed", "runs": changed_runs})

    current_project_hash = ""
    if project is not None:
        current_project_hash = _canonical_hash(_project_snapshot(project))
        if current_project_hash != str(metadata.get("project_snapshot_hash") or ""):
            reasons.append({"code": "project_changed"})

    return {
        "stale": bool(reasons),
        "stale_reasons": reasons,
        "current_source_snapshot_hash": current["source_snapshot_hash"],
        "current_project_snapshot_hash": current_project_hash,
        "current_included_run_count": current["included_run_count"],
        "current_excluded_runs": current["excluded_runs"],
        "current_total_run_count": current["total_run_count"],
    }


def _project_dir(project_id: str, storage_root: Path | None) -> Path:
    return reports_root(storage_root) / _safe_project_id(project_id)


def list_project_reports(
    project_id: str,
    *,
    run_dirs: Iterable[Path] | None = None,
    project: Mapping[str, Any] | None = None,
    storage_root: Path | None = None,
) -> list[dict[str, Any]]:
    """List ready/corrupt report metadata newest-first, without artifact content."""
    dirs = list(run_dirs) if run_dirs is not None else None
    reports: list[dict[str, Any]] = []
    for _, version_dir in _version_dirs(_project_dir(project_id, storage_root)):
        metadata = _load_version(version_dir, include_content=False)
        if metadata is None:
            continue
        if dirs is not None:
            metadata.update(calculate_staleness(metadata, dirs, project=project))
        reports.append(metadata)
    return reports


def _normalize_version(version: int | str) -> int:
    if isinstance(version, int):
        value = version
    else:
        raw = str(version).strip()
        match = _VERSION_RE.fullmatch(raw)
        value = int(match.group(1)) if match else int(raw)
    if value < 1:
        raise ValueError("version must be positive")
    return value


def read_project_report(
    project_id: str,
    version: int | str | None = None,
    *,
    run_dirs: Iterable[Path] | None = None,
    project: Mapping[str, Any] | None = None,
    storage_root: Path | None = None,
) -> dict[str, Any] | None:
    """Read one report version (latest when omitted), including Markdown content."""
    project_dir = _project_dir(project_id, storage_root)
    if version is None:
        versions = _version_dirs(project_dir)
        if not versions:
            return None
        version_dir = versions[0][1]
    else:
        number = _normalize_version(version)
        version_dir = project_dir / f"v{number:04d}"

    metadata = _load_version(version_dir, include_content=True)
    if metadata is None:
        return None
    if run_dirs is not None:
        metadata.update(calculate_staleness(metadata, run_dirs, project=project))
    return metadata


def project_report_status(
    project_id: str,
    run_dirs: Iterable[Path],
    *,
    project: Mapping[str, Any] | None = None,
    storage_root: Path | None = None,
) -> dict[str, Any]:
    """Return a compact UI-ready status for the latest project report."""
    dirs = list(run_dirs)
    reports = list_project_reports(
        project_id,
        run_dirs=dirs,
        project=project,
        storage_root=storage_root,
    )
    if not reports:
        current = source_snapshot(dirs)
        return {
            "status": "none",
            "ready": False,
            "stale": False,
            "version_count": 0,
            "latest": None,
            "current_included_run_count": current["included_run_count"],
            "current_excluded_runs": current["excluded_runs"],
            "current_total_run_count": current["total_run_count"],
        }

    latest = reports[0]
    if not latest.get("ready"):
        status = "corrupt"
    elif latest.get("stale"):
        status = "outdated"
    else:
        status = "ready"
    return {
        "status": status,
        "ready": bool(latest.get("ready")),
        "stale": bool(latest.get("stale")),
        "version_count": len(reports),
        "latest": latest,
        "current_included_run_count": latest.get("current_included_run_count"),
        "current_excluded_runs": latest.get("current_excluded_runs", []),
        "current_total_run_count": latest.get("current_total_run_count"),
    }
