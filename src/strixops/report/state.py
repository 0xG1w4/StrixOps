"""Single owner of mutable run state: findings, run record, completion.

Rules the platform depends on:

* ``run.completed`` is emitted **exactly once** per run — a guard flag makes
  double ``mark_complete()`` calls idempotent (the platform's reporting
  promotion keys on this event; duplicates corrupt its state machine).
* Finding ids sequence ``vuln-0001…`` / ``int-0001…``; timestamps use the
  artifacts module's UTC stamp format.
* ``run.json`` is the authoritative run record; ``status`` moves
  ``running → completed | failed``.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strixops.engine.targets import normalize_targets
from strixops.platform import artifacts
from strixops.platform.events import EventWriter
from strixops.report.assessment import AssessmentState
from strixops.report.assessment import summary as assessment_summary
from strixops.report.evidence import collect_files, finding_references
from strixops.report.notes import NotesStore

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
INTERRUPTED = "interrupted"


class DuplicateDependencyReport(ValueError):
    def __init__(self, report_id: str) -> None:
        super().__init__(f"Dependency finding already exists: {report_id}")
        self.report_id = report_id


# -- evidence helpers (module level) -------------------------------------------


SEVERITIES = tuple(artifacts.SEVERITY_ORDER)


def normalize_severity(value: str) -> str:
    """Canonical severity shared by internal reporting and lifecycle tools."""
    severity = value.strip().lower()
    if severity == "informational":
        severity = "info"
    if severity and severity not in SEVERITIES:
        raise ValueError("severity must be one of critical|high|medium|low|info")
    return severity


_EVIDENCE_CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        (
            "secretsdump",
            "ntds",
            "sam",
            "system",
            "security",
            "dcsync",
            "hash",
            "ntlm",
            "kirbi",
            "ccache",
            "gpp",
        ),
        "credential_dump",
    ),
    (("cred", "password", "passwd", "shadow"), "credential"),
    (("ssh", "id_rsa", "id_ed25519", "authorized_keys", "known_hosts"), "ssh_key"),
    (("aws", "gcp", "azure", "kube", "docker", "vault"), "cloud_credential"),
    (("wallet", "seed", "keystore", "private", "bitcoin", "ethereum", "monero"), "crypto"),
    (("config", "env", "settings", ".env", "server.xml", "web.config", "appsettings"), "config"),
    (("bloodhound", "sharphound", "recon", "scan", "enum"), "recon_data"),
    ((".sql", ".db", ".sqlite", "dump.sql", "database"), "database"),
]


def _infer_evidence_category(filename: str) -> str:
    """Infer evidence category from filename patterns."""
    name = filename.lower()
    for keywords, category in _EVIDENCE_CATEGORY_RULES:
        if any(kw in name for kw in keywords):
            return category
    if name.endswith((".csv", ".txt", ".log", ".md", ".json", ".xml", ".yml", ".yaml")):
        return "data"
    return "other"


class RunState:
    def __init__(self, run_dir: Path, events: EventWriter) -> None:
        self.run_dir = Path(run_dir)
        self.events = events
        self._lock = threading.RLock()
        self._notes = NotesStore(self.run_dir, self._lock)
        self.assessment = AssessmentState(self.save, self._lock)
        self.reports: list[dict[str, Any]] = []
        self.internal_findings: list[dict[str, Any]] = []
        self.run_record: dict[str, Any] = {
            "run_name": self.run_dir.name,
            "status": RUNNING,
            "start_time": datetime.now(UTC).isoformat(),
        }
        self._start_monotonic = time.monotonic()
        self._completed_emitted = False
        self._final_fields: dict[str, Any] | None = None

    @property
    def notes(self) -> NotesStore:
        """One lazy note store shared by this run's root and children."""
        return self._notes

    # -- lifecycle ---------------------------------------------------------

    def set_scan_config(self, scan_config: dict[str, Any]) -> None:
        self.run_record["scan_config"] = scan_config
        self.save()

    def record_usage(self, agent_id: str, totals: dict[str, int]) -> None:
        """Record run-level token usage after each completed model response.

        Writes only ``run.json`` (artifacts are untouched) and emits a
        ``usage.updated`` event so the console can show live spend. Unknown
        event types are ignored gracefully by both the platform parser and
        the console parser.
        """
        self.run_record["llm_usage"] = dict(totals)
        artifacts.write_run_record(self.run_dir, self.run_record)
        self.events.emit(
            event_type="usage.updated",
            payload={"agent_id": agent_id, **totals},
        )

    def update_final_fields(
        self,
        *,
        executive_summary: str,
        methodology: str,
        technical_analysis: str,
        recommendations: str,
        overall_severity: str = "",
        severity_rationale: str = "",
        battle_gains: str = "",
        attack_narrative: str = "",
        environment_map: str = "",
        credential_capabilities: str = "",
        future_leverage: str = "",
        business_impact: str = "",
        limitations: str = "",
    ) -> None:
        self._final_fields = {
            "executive_summary": executive_summary,
            "methodology": methodology,
            "technical_analysis": technical_analysis,
            "recommendations": recommendations,
            "overall_severity": normalize_severity(overall_severity),
            "severity_rationale": severity_rationale,
            "battle_gains": battle_gains,
            "attack_narrative": attack_narrative,
            "environment_map": environment_map,
            "credential_capabilities": credential_capabilities,
            "future_leverage": future_leverage,
            "business_impact": business_impact,
            "limitations": limitations,
        }

    def duration_seconds(self) -> int:
        return int(time.monotonic() - self._start_monotonic)

    @property
    def final_fields(self) -> dict[str, Any] | None:
        """Read-only copy of the finish_scan narrative, for report synthesis."""
        if self._final_fields is None:
            return None
        return dict(self._final_fields)

    def mark_complete(self) -> None:
        self.run_record["status"] = COMPLETED
        self.run_record["end_time"] = datetime.now(UTC).isoformat()
        self.run_record["duration_seconds"] = self.duration_seconds()
        if self._final_fields is not None:
            self._final_fields["overall_severity"] = (
                self._final_fields["overall_severity"] or self._derived_severity()
            )
            self.run_record["scan_results"] = dict(self._final_fields, success=True)
        self.save()
        self._emit_run_completed()

    def mark_failed(self, reason: str = "") -> None:
        self.run_record["status"] = FAILED
        self.run_record["end_time"] = datetime.now(UTC).isoformat()
        self.run_record["duration_seconds"] = self.duration_seconds()
        if reason:
            self.run_record["failure_reason"] = reason
        self.save()

    def _emit_run_completed(self) -> None:
        if self._completed_emitted:
            return
        self._completed_emitted = True
        self.events.emit(
            event_type="run.completed",
            payload={
                "duration_seconds": self.run_record.get("duration_seconds", 0),
                "vulnerability_count": len(self.reports),
            },
        )

    # -- findings ----------------------------------------------------------

    def add_vulnerability_report(
        self,
        report: dict[str, Any],
        *,
        agent_id: str,
        agent_name: str,
    ) -> dict[str, Any]:
        with self._lock:
            if report.get("finding_class") == "dependency_cve":

                def identity(item: dict) -> tuple:
                    metadata = item.get("dependency_metadata") or {}
                    return (
                        item.get("target"),
                        item.get("cve"),
                        metadata.get("package_name"),
                        metadata.get("package_ecosystem"),
                        metadata.get("manifest_path"),
                    )

                duplicate = next(
                    (
                        old
                        for old in self.reports
                        if old.get("finding_class") == "dependency_cve" and identity(old) == identity(report)
                    ),
                    None,
                )
                if duplicate is not None:
                    raise DuplicateDependencyReport(duplicate["id"])
            report = copy.deepcopy(report)
            report.setdefault("id", f"vuln-{len(self.reports) + 1:04d}")
            report.setdefault("timestamp", artifacts.utc_stamp())
            report.setdefault("agent_id", agent_id)
            report.setdefault("agent_name", agent_name)
            self.reports.append(report)
            try:
                self.save()
            except Exception:
                self.reports.pop()
                raise
            self.events.vulnerability_found(
                finding={k: v for k, v in report.items() if k not in ("poc_script_code",)},
                report_id=report["id"],
                agent_id=agent_id,
                agent_name=agent_name,
            )
            return copy.deepcopy(report)

    def read_reports(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.reports)

    def revise_vulnerability_report(
        self,
        report_id: str,
        updates: dict[str, Any],
        *,
        reason: str,
        agent_id: str,
        agent_name: str,
        validate: Callable[[dict[str, Any]], list[str]] | None = None,
    ) -> dict[str, Any]:
        """Apply an already-validated revision and emit one additive update event.

        No await separates reading the old record from replacement. The shared
        lock also serializes tool calls from worker threads. Returned data is a
        copy, so readers cannot mutate the owner or its audit history.
        """
        if not reason.strip():
            raise ValueError("update_reason must be non-empty")
        with self._lock:
            index = next((i for i, r in enumerate(self.reports) if r["id"] == report_id), None)
            if index is None:
                raise ValueError(f"Report {report_id} not found")
            old = self.reports[index]
            changed = {k: copy.deepcopy(v) for k, v in updates.items() if old.get(k) != v}
            if not changed:
                raise ValueError("No changed fields to update")
            revised = copy.deepcopy(old)
            revised.update(changed)
            if validate is not None and (errors := validate(revised)):
                raise ValueError("Validation failed: " + "; ".join(errors))
            now = artifacts.utc_stamp()
            revised.setdefault("update_history", []).append(
                {
                    "reason": reason.strip(),
                    "fields": list(changed),
                    "dropped_fields": [],
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "timestamp": now,
                    "previous": {k: copy.deepcopy(old.get(k)) for k in changed},
                }
            )
            revised["updated_at"] = now
            self.reports[index] = revised
            try:
                self.save()
            except Exception:
                self.reports[index] = old
                raise
            self.events.emit(
                event_type="vulnerability.updated",
                agent_id=agent_id,
                agent_name=agent_name,
                payload={
                    "report_id": report_id,
                    "finding": {k: copy.deepcopy(v) for k, v in revised.items() if k != "poc_script_code"},
                },
            )
            return copy.deepcopy(revised)

    def add_internal_finding(
        self,
        finding: dict[str, Any],
        *,
        agent_id: str,
        agent_name: str,
    ) -> dict[str, Any]:
        with self._lock:
            finding = copy.deepcopy(finding)
            finding.setdefault("id", f"int-{len(self.internal_findings) + 1:04d}")
            finding.setdefault("timestamp", artifacts.utc_stamp())
            finding.setdefault("agent_id", agent_id)
            finding.setdefault("agent_name", agent_name)
            self.internal_findings.append(finding)
            try:
                self.save()
            except Exception:
                self.internal_findings.pop()
                raise
            self.events.internal_finding_created(finding=finding, agent_id=agent_id, agent_name=agent_name)
            return copy.deepcopy(finding)

    # -- persistence -------------------------------------------------------

    def save(self) -> None:
        with self._lock:
            assessment = self.assessment.snapshot(str(self.run_record.get("status") or "unknown"))
            artifacts.atomic_write_text(
                self.run_dir / "assessment.json",
                json.dumps(assessment, ensure_ascii=False, indent=2, allow_nan=False),
            )
            self.run_record["assessment"] = assessment_summary(assessment)
            artifacts.write_run_record(self.run_dir, self.run_record)
            if self.reports:
                artifacts.write_vulnerabilities(self.run_dir, self.reports)
            if self.internal_findings:
                artifacts.write_internal_findings(self.run_dir, self.internal_findings)

    # -- evidence collection ------------------------------------------------

    def collect_evidence(self, workspace_dir: Path) -> int:
        """Persist all regular output files; count only delivered attachments.

        The workspace is retained independently, including on copy failures.
        Manifest entries distinguish capture, persistence, and delivery.
        Finding references are diagnostic only: an unwritten attachment must
        not invalidate files that were successfully saved or fail the run.
        """
        index, errors = collect_files(
            Path(workspace_dir), self.run_dir / "evidence", _infer_evidence_category
        )
        references = finding_references([*self.reports, *self.internal_findings], index)
        delivered = [entry for entry in index if entry["deliverable"]]
        missing = [ref for ref in references if not ref["deliverable"]]
        self.run_record["evidence"] = {
            "status": "incomplete" if errors or len(delivered) != len(index) else "complete",
            "count": len(delivered),
            "captured_count": sum(entry["captured"] for entry in index),
            "persisted_count": sum(entry["persisted"] for entry in index),
            "failed_count": len(index) - len(delivered),
            "total_bytes": sum(entry["size"] for entry in delivered),
            "files": [entry["filename"] for entry in delivered],
            **(
                {"manifest": "evidence/.evidence_index.json"}
                if not (self.run_dir / "evidence").is_symlink()
                and (self.run_dir / "evidence" / ".evidence_index.json").is_file()
                else {}
            ),
            "references": references,
            "missing_reference_count": len(missing),
            "errors": errors,
        }
        self.save()
        return len(delivered)

    def report_language(self) -> str:
        scan_config = self.run_record.get("scan_config") or {}
        return str(scan_config.get("report_language") or "zh-CN")

    def write_executive_report(self) -> None:
        """Compose the client-facing report in the platform's deliverable format.

        Header block (target/type/time/overall severity/rationale), then the
        themed sections the platform's reports mandate — filled from the
        structured finish_scan fields, findings table, and internal findings.
        Empty sections are omitted, mirroring "include when evidence exists".
        """
        fields = self._final_fields
        if not fields:
            return
        zh = self.report_language().startswith("zh")

        scan_config = self.run_record.get("scan_config") or {}
        target = str(scan_config.get("target") or self.run_dir.name)
        try:
            targets = normalize_targets(scan_config.get("target", ""), scan_config.get("targets"))
        except ValueError:
            targets = []
        multi_target = len(targets) > 1
        scan_type = str(scan_config.get("scan_type") or "web")
        if zh:
            task_type_label = "内网渗透测试" if scan_type == "internal" else "Web应用渗透测试"
        else:
            task_type_label = (
                "Internal network penetration test"
                if scan_type == "internal"
                else "Web application penetration test"
            )

        severity = (fields.get("overall_severity") or self._derived_severity() or "Info").title()
        rationale = fields.get("severity_rationale") or ""

        zh_sections = [
            ("执行摘要", "executive_summary"),
            ("本阶段战果整理", "battle_gains"),
            ("攻击路径与关键进展", "attack_narrative"),
        ]
        zh_sections_internal = [
            ("内网架构、关键主机与服务", "environment_map"),
            ("凭证、哈希与存取能力", "credential_capabilities"),
            ("后续可利用路径", "future_leverage"),
            ("敏感数据与业务冲击", "business_impact"),
        ]
        zh_sections += [
            ("重要发现与技术细节", "technical_analysis"),
            ("本阶段限制与未完成部分", "limitations"),
        ]
        # Theme sections render for every scan type — a web engagement with
        # credentials, observed architecture or follow-on leverage files those
        # fields too. Empty sections are skipped below, so a bare web run
        # reads exactly as before.
        all_sections = zh_sections[:3] + zh_sections_internal + zh_sections[3:]

        if zh:
            labels = {
                "target": "目标",
                "type": "任务类型",
                "generated": "报告产生时间",
                "sections": all_sections,
                "methodology": "测试方法与覆盖范围",
                "recommendations": "修复建议",
                "findings": "漏洞清单",
                "internal": "发现清单",
                "no_findings": "本次扫描未发现已验证的漏洞。",
                "no_internal": "本次扫描未记录内网发现。",
            }
            title = f"渗透测试报告 - {len(targets)} 个目标" if multi_target else f"渗透测试报告 - {target}"
        else:
            en_sections = [
                ("Executive Summary", "executive_summary"),
                ("Engagement Gains", "battle_gains"),
                ("Attack Path & Key Progress", "attack_narrative"),
            ]
            en_sections_internal = [
                ("Environment, Key Hosts & Services", "environment_map"),
                ("Credentials, Hashes & Access", "credential_capabilities"),
                ("Follow-on Leverage", "future_leverage"),
                ("Sensitive Data & Business Impact", "business_impact"),
            ]
            en_sections += [
                ("Findings & Technical Detail", "technical_analysis"),
                ("Limitations & Unfinished Work", "limitations"),
            ]
            en_all = en_sections[:3] + en_sections_internal + en_sections[3:]
            labels = {
                "target": "Target",
                "type": "Engagement type",
                "generated": "Generated at",
                "sections": en_all,
                "methodology": "Methodology & Coverage",
                "recommendations": "Remediation Recommendations",
                "findings": "Vulnerability Index",
                "internal": "Findings",
                "no_findings": "No validated vulnerabilities were found in this scan.",
                "no_internal": "No internal findings were recorded.",
            }
            title = (
                f"Penetration Test Report - {len(targets)} targets"
                if multi_target
                else f"Penetration Test Report - {target}"
            )

        generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Header block: every line separated by a blank line so Markdown
        # renders each as its own paragraph (not one run-on blob).
        target_lines = (
            [
                f"目标（{len(targets)}）：" if zh else f"Targets ({len(targets)}):",
                "",
                *[f"- {value}" for value in targets],
            ]
            if multi_target
            else [f"{labels['target']}：{target}" if zh else f"{labels['target']}: {target}"]
        )
        lines: list[str] = [
            f"# {title}",
            "",
            *target_lines,
            "",
            f"{labels['type']}：{task_type_label}" if zh else f"{labels['type']}: {task_type_label}",
            "",
            f"{labels['generated']}：{generated}" if zh else f"{labels['generated']}: {generated}",
            "",
            f"Overall Severity: {severity}",
            "",
        ]
        if self.run_record.get("status") in (FAILED, INTERRUPTED):
            lines += ["Report Status: Partial (run failed or interrupted)", ""]
        if rationale:
            lines += [f"Severity Rationale: {rationale}", ""]
        lines += ["---", ""]

        for heading, key in labels["sections"]:
            content = (fields.get(key) or "").strip()
            if not content:
                continue
            lines += [f"## {heading}", "", content, ""]

        methodology = (fields.get("methodology") or "").strip()
        if methodology:
            lines += [f"## {labels['methodology']}", "", methodology, ""]

        assessment = self.assessment.snapshot(str(self.run_record.get("status") or "unknown"))
        coverage = assessment["coverage"]
        lines += ["## 覆盖记录与未解决事项" if zh else "## Coverage Records & Unresolved Items", ""]
        if zh:
            lines += ["覆盖记录是 Agent 对已检查项目的陈述；执行完成不代表全面覆盖或不存在风险。", ""]
            if not coverage["entries"]:
                lines += ["未记录结构化覆盖信息，覆盖范围未知。", ""]
            else:
                lines += [
                    f"已记录 {len(coverage['entries'])} 项；其中 {coverage['unresolved_count']} 项仍需跟进。",
                    "",
                ]
        else:
            lines += [
                "Coverage is agent-reported. Execution completion does not prove "
                "exhaustive coverage or safety.",
                "",
            ]
            if not coverage["entries"]:
                lines += ["No structured coverage was recorded; coverage is unknown.", ""]
            else:
                lines += [
                    f"Recorded items: {len(coverage['entries'])}; "
                    f"unresolved: {coverage['unresolved_count']}.",
                    "",
                ]
        for entry in coverage["entries"]:
            if entry["outcome"] == "needs_follow_up":
                lines += [f"- `{entry['id']}` {entry['surface']} — {entry['risk_area']}: {entry['evidence']}"]
        lines += ["", "[Assessment details and history](assessment.json)", ""]

        if self.reports:
            lines += [f"## {labels['findings']}", ""]
            for report in self.reports:
                sev = str(report.get("severity") or "info").upper()
                title2 = report.get("title") or report.get("id") or "?"
                rid = report.get("id") or ""
                lines.append(f"- **[{sev}]** {title2} (`{rid}`)")
            lines.append("")

        # Findings index: shown in all modes (tool is universal now)
        if self.internal_findings:
            lines += [f"## {labels['internal']}", ""]
            for finding in self.internal_findings:
                ftype = finding.get("finding_type", "result")
                lines.append(f"- **[{ftype}]** {finding.get('title', finding.get('id', '?'))}")
            lines.append("")

        evidence = self.run_record.get("evidence")
        if evidence is not None:
            heading = "已保存证据" if zh else "Saved Evidence"
            lines += [f"## {heading}", ""]
            if zh:
                lines += [
                    f"已保存 {evidence['count']} 个附件。"
                    if evidence["count"] else "本次任务没有已保存的附件。",
                    "",
                ]
            else:
                lines += [
                    f"Saved attachments: {evidence['count']}."
                    if evidence["count"] else "No attachments were saved for this run.",
                    "",
                ]
            if evidence["count"] and evidence.get("manifest"):
                lines += ["[Evidence manifest](evidence/.evidence_index.json)", ""]

        recommendations = (fields.get("recommendations") or "").strip()
        if recommendations:
            lines += [f"## {labels['recommendations']}", "", recommendations, ""]

        artifacts.write_executive_report(self.run_dir, "\n".join(lines).rstrip() + "\n")

    def _derived_severity(self) -> str:
        """Highest finding severity when finish_scan did not state one."""
        order = artifacts.SEVERITY_ORDER
        best = "info"
        for report in [*self.reports, *self.internal_findings]:
            sev = str(report.get("severity") or "info").strip().lower()
            if sev == "informational":
                sev = "info"
            if sev in order and order[sev] < order.get(best, 4):
                best = sev
        return best
