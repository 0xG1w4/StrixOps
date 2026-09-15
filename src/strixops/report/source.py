"""Fit finding records and run context into a report input budget, without attachment bodies."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from strixops.engine.targets import normalize_targets
from strixops.report.source_evidence import referenced_evidence_names
from strixops.report.state import RunState

_DEFAULT_SOURCE_TOKENS = 180_000
_EVIDENCE_FILE_LIMIT = 200
_EVIDENCE_CHAR_BUDGET = 20_000


class ReportSourceTooLarge(ValueError):
    """Even the run scope and finding identities cannot fit the model input."""


_REPORT_FIELDS = (
    "id",
    "title",
    "severity",
    "cvss",
    "cvss_vector",
    "description",
    "impact",
    "target",
    "endpoint",
    "method",
    "cve",
    "cwe",
    "technical_analysis",
    "poc_description",
    "poc_script_code",
    "evidence",
    "counterevidence",
    "confidence",
    "confidence_rationale",
    "severity_change_conditions",
    "fix_effort",
    "finding_class",
    "metadata",
    "dependency_metadata",
    "timestamp",
    "assumptions",
    "remediation_steps",
    "code_locations",
    "fix_verification",
    "fix_pr_body",
    "cvss_breakdown",
    "update_history",
)

_FINDING_FIELDS = (
    "id",
    "finding_type",
    "title",
    "content",
    "host",
    "source",
    "severity",
    "metadata",
    "agent_name",
    "timestamp",
)


def _selected(payload: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key in fields and value not in ("", None, [], {})}


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _saved_evidence_inventory(run_state: RunState) -> dict[str, Any]:
    """Expose delivered files only; finding references are not attachments."""
    evidence = run_state.run_record.get("evidence") or {}
    names = evidence.get("files", []) if isinstance(evidence, dict) else []
    if not isinstance(names, list):
        names = []
    files: list[dict[str, str]] = []
    candidates: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    remaining = _EVIDENCE_CHAR_BUDGET
    for name in names:
        if not isinstance(name, str) or not name:
            continue
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
            continue
        filename = path.as_posix()
        if filename in seen:
            continue
        seen.add(filename)
        entry = {"filename": filename, "link": "evidence/" + quote(filename, safe="/")}
        candidates[filename] = entry
    referenced = referenced_evidence_names(run_state, list(candidates.values()))
    ordered = dict.fromkeys([*referenced, *candidates])
    for filename in ordered:
        entry = candidates[filename]
        # Omit whole entries instead of truncating filenames or breaking links.
        size = len(_dump_json(entry)) + 8
        if len(files) < _EVIDENCE_FILE_LIMIT and size <= remaining:
            files.append(entry)
            remaining -= size
    return {
        "saved_file_count": len(seen),
        "files": files,
        "omitted_from_source_count": len(seen) - len(files),
    }


@dataclass
class _Record:
    heading: str
    source: str
    values: dict[str, Any]
    kept: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        return self.heading + "\n" + _dump_json(self.kept) + "\n\n"


def _overview(run_state: RunState, generated_at: str) -> dict[str, Any]:
    config = run_state.run_record.get("scan_config") or {}
    overview = {
        "target": config.get("target") or run_state.run_dir.name,
        "scan_type": config.get("scan_type") or "web",
        "crypto_mode": config.get("crypto_mode", False),
        "report_language": config.get("report_language") or "zh-CN",
        "run_status": str(run_state.run_record.get("status") or "unknown"),
        "duration_seconds": run_state.duration_seconds(),
        "generated_at": generated_at,
        "vulnerability_report_count": len(run_state.reports),
        "internal_finding_count": len(run_state.internal_findings),
    }
    if "targets" in config:
        targets = normalize_targets(config.get("target", ""), config["targets"])
        if len(targets) > 1:
            overview.update(targets=targets, target_count=len(targets))
    if config.get("socks5_proxy"):
        overview["declared_access"] = f"socks5: {config['socks5_proxy']}"
    elif config.get("gsocket_key"):
        overview["declared_access"] = "gsocket: <key supplied>"
    return overview


def build_report_source(
    run_state: RunState,
    *,
    generated_at: str,
    trimmed: bool = False,
    token_budget: int = _DEFAULT_SOURCE_TOKENS,
    token_count: Callable[[str], int] | None = None,
) -> str:
    """Keep whole values, explain omissions, and enforce the final token budget.

    Findings retain their identities even under pressure. Fields are indivisible:
    a credential, PoC or response is included verbatim or explicitly omitted.
    Retry drops supplemental context, never wholesale internal-finding bodies.
    Inline evidence and PoC fields remain source material; raw files under
    evidence/ are never opened. The attachment inventory contains links only.
    The caller supplies its model tokenizer; direct/offline use conservatively
    counts UTF-8 bytes. This function never mutates saved findings or evidence.
    """
    count = token_count or (lambda text: len(text.encode("utf-8")))
    if token_budget <= 0:
        raise ReportSourceTooLarge("No report input budget remains")
    overview = _overview(run_state, generated_at)
    inventory = _saved_evidence_inventory(run_state)
    # Inventory is supporting metadata. Reserve input for the actual findings.
    inventory_limit = max(0, token_budget // 6)
    while inventory["files"] and count(_dump_json(inventory)) > inventory_limit:
        inventory["files"].pop()
        inventory["omitted_from_source_count"] += 1
    prefix = (
        "# REPORT SOURCE\n\n## Run Overview\n" + _dump_json(overview)
        + "\n\n## Saved Evidence Attachments (authoritative delivery inventory)\n"
        + _dump_json(inventory) + "\n\n"
    )
    records: list[_Record] = []
    for heading, rows, fields in (
        ("Vulnerability Reports", run_state.reports, _REPORT_FIELDS),
        ("Internal Findings", run_state.internal_findings, _FINDING_FIELDS),
    ):
        for index, row in enumerate(rows, 1):
            values = _selected(row, fields)
            identity = {
                key: values[key] for key in ("id", "title", "severity", "finding_type") if key in values
            }
            records.append(_Record(
                f"## {heading}\n\n### {values.get('id') or index}",
                str(values.get("id") or f"{heading}:{index}"), values, identity,
            ))
    core_count = len(records)
    draft = run_state.final_fields
    if draft:
        records.append(_Record(
            "## Root Agent Draft Narrative (finish_scan fields — leads, verify against the filed reports)",
            "root_draft", {key: value for key, value in draft.items() if value not in ("", None, [], {})},
        ))
    extras: list[tuple[str, str]] = []
    supplemental_omissions: list[dict[str, Any]] = []
    accepted: list[tuple[_Record, str]] = []
    # Charge complete serialized units conservatively, then verify the exact
    # assembled input. Token boundaries across units need not be additive.
    coverage_reserve = min(4096, max(256, token_budget // 10))
    remaining = token_budget - count(prefix + "".join(row.render() for row in records)) - coverage_reserve
    if remaining < 0:
        raise ReportSourceTooLarge("Run scope and finding identities exceed the report input budget")

    def admit_fields(record: _Record, allowance: int) -> None:
        nonlocal remaining
        for key, value in record.values.items():
            if key in record.kept:
                continue
            cost = count(_dump_json({key: value})) + 8
            if cost <= min(remaining, allowance):
                record.kept[key] = value
                accepted.append((record, key))
                remaining -= cost
                allowance -= cost

    # A large early finding must not consume every later finding's allocation.
    share = int(remaining * 0.7) // max(1, core_count)
    for row in records[:core_count]:
        admit_fields(row, share)
    # Reuse spare shares for primary evidence before any supporting material.
    for row in records[:core_count]:
        admit_fields(row, remaining)
    for row in records[core_count:]:
        admit_fields(row, remaining // 3)

    # Use remaining room for complete primary fields before ledgers/coverage.
    for row in records:
        admit_fields(row, remaining)
    supplemental: list[tuple[str, str, Any]] = []
    campaign = run_state.run_record.get("internal_campaign")
    if campaign:
        supplemental.append((
            "campaign", "Internal Campaign Ledger (engagement-created resources and observations)", campaign,
        ))
    try:
        coverage = run_state.assessment.snapshot(overview["run_status"])["coverage"]
    except Exception:  # Optional assessment must not block report generation.
        coverage = None
    if coverage:
        supplemental.append(("coverage", "Coverage Records (agent-reported per-surface outcomes)", coverage))
    for key, heading, value in supplemental:
        text = f"## {heading}\n" + _dump_json(value) + "\n\n"
        cost = count(text)
        if not trimmed and cost <= remaining:
            extras.append((key, text))
            remaining -= cost
        else:
            supplemental_omissions.append({
                "source": key, "reason": "retry_source" if trimmed else "input_token_budget",
            })

    def render() -> str:
        omitted_fields = [
            {"source": row.source, "field": key, "reason": "input_token_budget"}
            for row in records for key in row.values if key not in row.kept
        ]
        omissions = omitted_fields + supplemental_omissions
        audit = {
            "input_token_budget": token_budget,
            "finding_count": core_count,
            "omitted_field_count": len(omitted_fields),
            "evidence_content_policy": "attachments_not_loaded",
            "evidence_block_count": 0,
            "evidence_excerpt_count": 0,
            "omission_count": len(omissions),
            "omissions": omissions,
            "omission_details_not_listed": 0,
        }
        while audit["omissions"] and count(_dump_json(audit)) > coverage_reserve:
            audit["omissions"] = audit["omissions"][:-1]
            audit["omission_details_not_listed"] += 1
        content = prefix + "".join(row.render() for row in records)
        content += "".join(text for _, text in extras)
        return content + "## Source Coverage\n" + _dump_json(audit) + "\n"

    source = render()
    while count(source) > token_budget:
        if extras:
            key, _ = extras.pop()
            supplemental_omissions.append({"source": key, "reason": "input_token_budget"})
        elif accepted:
            row, key = accepted.pop()
            del row.kept[key]
        else:
            raise ReportSourceTooLarge("Run scope and finding identities exceed the report input budget")
        source = render()
    return source
