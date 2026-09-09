"""Reporting tools: vulnerability reports (web) and internal findings."""

from __future__ import annotations

import copy
import json
import math
import re
from pathlib import PurePosixPath
from typing import Any

from agents import RunContextWrapper, function_tool

from strixops.engine.scanconfig import EngineContext
from strixops.report.cvss import calculate_cvss, validate_cvss_breakdown
from strixops.report.dedupe import check_duplicate
from strixops.report.state import DuplicateDependencyReport, RunState, normalize_severity


def _relative_path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and not value.startswith(("/", "./"))
        and "\\" not in value
        and not re.match(r"^[A-Za-z]:", value)
        and not any(ord(c) < 32 for c in value)
        and not any(part in {".", ".."} for part in value.split("/"))
        and not PurePosixPath(value).is_absolute()
    )


def _validate_rich_fields(report: dict[str, Any]) -> list[str]:
    """Validate optional structured evidence without changing legacy required args."""
    errors: list[str] = []
    if not str(report.get("title") or "").strip():
        errors.append("title must be non-empty")
    effort = str(report.get("fix_effort") or "").strip().lower()
    if effort not in {"trivial", "low", "moderate", "medium", "high"}:
        errors.append("fix_effort must be trivial|low|moderate|medium|high")
    confidence = str(report.get("confidence") or "").strip().lower()
    rationale = str(report.get("confidence_rationale") or "").strip()
    if confidence in {"medium", "low"} and (not rationale or rationale.lower() == confidence):
        errors.append("confidence_rationale must name the evidence gap for medium/low confidence")
    if report.get("finding_class", "dynamic") not in {"dynamic", "dependency_cve"}:
        errors.append("finding_class must be dynamic|dependency_cve")
    locations = report.get("code_locations") or []
    if not isinstance(locations, list):
        return errors + ["code_locations must be an array"]
    actionable = False
    for index, loc in enumerate(locations):
        prefix = f"code_locations[{index}]"
        if not isinstance(loc, dict):
            errors.append(f"{prefix} must be an object")
            continue
        allowed = {"file", "start_line", "end_line", "snippet", "label", "fix_before", "fix_after"}
        if set(loc) - allowed:
            errors.append(f"{prefix} has unsupported fields: {sorted(set(loc) - allowed)}")
        if not _relative_path(loc.get("file")):
            errors.append(f"{prefix}.file must be a repository-relative path without '.' or '..'")
        start, end = loc.get("start_line"), loc.get("end_line")
        valid_span = type(start) is int and type(end) is int and start > 0 and end >= start
        if not valid_span:
            errors.append(f"{prefix} requires positive start_line and end_line >= start_line")
        for name in ("snippet", "label", "fix_before", "fix_after"):
            if name in loc and not isinstance(loc[name], str):
                errors.append(f"{prefix}.{name} must be a string")
        if ("fix_before" in loc) != ("fix_after" in loc):
            errors.append(f"{prefix} requires both fix_before and fix_after")
        if "fix_after" in loc:
            actionable = True
            before = loc.get("fix_before")
            if not isinstance(before, str) or not before:
                errors.append(f"{prefix}.fix_before must contain the original source block")
            elif valid_span and len(before.splitlines()) != end - start + 1:
                errors.append(f"{prefix}.fix_before line count must match start_line..end_line")
    if actionable and not str(report.get("fix_verification") or "").strip():
        errors.append("fix_verification is required for actionable code_locations")
    metadata = report.get("dependency_metadata")
    if metadata is not None:
        allowed = {
            "package_name",
            "installed_version",
            "package_ecosystem",
            "manifest_path",
            "fixed_version",
            "introduced_by",
            "dependency_path",
            "reachability",
            "reachability_evidence",
            "advisory_cvss",
            "contextual_cvss_breakdown",
            "contextual_cvss_score",
            "contextual_cvss_vector",
            "contextual_cvss_reasoning",
        }
        if not isinstance(metadata, dict):
            errors.append("dependency_metadata must be an object")
        else:
            if set(metadata) - allowed:
                errors.append("dependency_metadata contains unsupported fields")
            for name in ("package_name", "installed_version"):
                if not isinstance(metadata.get(name), str) or not metadata[name].strip():
                    errors.append(f"dependency_metadata.{name} must be non-empty")
            for name, value in metadata.items():
                if name in {"advisory_cvss", "contextual_cvss_breakdown", "contextual_cvss_score"}:
                    continue
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"dependency_metadata.{name} must be a non-empty string")
            if "manifest_path" in metadata and not _relative_path(metadata["manifest_path"]):
                errors.append("dependency_metadata.manifest_path must be repository-relative")
            reachability = metadata.get("reachability")
            if reachability is not None:
                if reachability not in {
                    "unknown",
                    "not_imported",
                    "imported",
                    "vulnerable_symbol_used",
                    "reachable_call_path",
                    "confirmed_exploitable",
                }:
                    errors.append("dependency_metadata.reachability is invalid")
                elif reachability != "unknown" and not metadata.get("reachability_evidence"):
                    errors.append("reachability_evidence is required when reachability is assessed")
    return errors


@function_tool(strict_mode=False)
async def create_vulnerability_report(
    ctx: RunContextWrapper[EngineContext],
    title: str,
    description: str,
    impact: str,
    target: str,
    technical_analysis: str,
    poc_description: str,
    poc_script_code: str,
    remediation_steps: str,
    evidence: str,
    assumptions: str,
    counterevidence: str,
    confidence: str,
    severity_change_conditions: str,
    fix_effort: str,
    cvss_breakdown: dict[str, str],
    endpoint: str = "",
    method: str = "",
    cve: str = "",
    cwe: str = "",
    confidence_rationale: str = "",
    code_locations: list[dict[str, Any]] | None = None,
    fix_verification: str = "",
    fix_pr_body: str = "",
) -> str:
    """File a validated vulnerability report. The ONLY way to record a web finding.

    Quality gates — a report that fails these will be rejected by reviewers:
      1. VALIDATION: Only report a vulnerability you have empirically verified
         with a working proof of concept. "Likely", "probable", and scanner
         output without manual confirmation are NOT findings — they are leads.
      2. COUNTEREVIDENCE: Actively try to disprove the vulnerability before
         filing. Record what you tried in `counterevidence`. If you could not
         break your own hypothesis, say so explicitly.
      3. SEVERITY CALIBRATION: Score CVSS metrics for THIS deployment's
         context (authentication required? network position? data
         sensitivity?), not the vulnerability class's theoretical worst case.
         Severity inflation destroys the report's credibility; deflation
         hides real risk.

    Args:
        title: Concise, specific finding title (e.g. "Stored XSS in user profile bio").
        description: What the vulnerability is and where exactly it occurs.
        impact: Concrete business/technical damage an attacker could achieve.
        target: The in-scope target this was found on.
        technical_analysis: Root cause: where in the application the flaw
            lives, why it exists, and how exploitation works.
        poc_description: Step-by-step reproduction narrative.
        poc_script_code: Runnable Python 3 PoC script (requests/socket/hashlib
            or other commonly available libs) — complete and executable on its
            own; never a bash one-liner.
        remediation_steps: Specific, actionable fixes.
        evidence: Raw observations proving exploitation (responses, outputs).
        assumptions: Anything you inferred but could not fully verify.
        counterevidence: What you tried that FAILED to disprove the finding.
        confidence: Your confidence level and why (evidence quality, PoC
            reliability, environmental factors).
        severity_change_conditions: What would raise or lower this severity
            in a different deployment context.
        fix_effort: Estimated remediation effort (trivial/low/moderate/high).
        cvss_breakdown: All 8 CVSS 3.1 metrics using these exact keys and
            values: attack_vector (N/A/L/P), attack_complexity (L/H),
            privileges_required (N/L/H), user_interaction (N/R), scope (U/C),
            confidentiality (N/L/H), integrity (N/L/H), availability (N/L/H).
        endpoint: Specific endpoint/path where the issue manifests, if narrower than target.
        method: HTTP method for the affected endpoint.
        cve: CVE identifier, if this finding maps to one.
        cwe: CWE identifier, if applicable.
        confidence_rationale: Evidence gap or explanation of confidence; required for medium/low.
        code_locations: Repository-relative file/start_line/end_line objects, optionally
            snippet/label and paired fix_before/fix_after blocks. Line counts must match.
        fix_verification: Verification of security closure and preserved behavior;
            required for suggested fixes. State what was tested and what remains untested.
        fix_pr_body: Optional reviewer-facing explanation of the proposed fix.
    """
    run_state: RunState = ctx.context.run_state  # type: ignore[assignment]
    errors = validate_cvss_breakdown(cvss_breakdown)
    if errors:
        return json.dumps({"success": False, "error": "Validation failed", "errors": errors})
    try:
        score, severity, vector = calculate_cvss(cvss_breakdown)
    except ValueError as exc:
        return json.dumps({"success": False, "error": "Validation failed", "errors": [str(exc)]})

    report: dict[str, Any] = {
        "title": title.strip(),
        "description": description,
        "impact": impact,
        "target": target,
        "technical_analysis": technical_analysis,
        "poc_description": poc_description,
        "poc_script_code": poc_script_code,
        "remediation_steps": remediation_steps,
        "evidence": evidence,
        "assumptions": assumptions,
        "counterevidence": counterevidence,
        "confidence": confidence,
        "confidence_rationale": confidence_rationale.strip() or confidence,
        "severity_change_conditions": severity_change_conditions,
        "fix_effort": fix_effort,
        "severity": severity,
        "cvss_breakdown": cvss_breakdown,
        "cvss": score,
        "cvss_vector": vector,
        "finding_class": "dynamic",
    }
    for key, value in (
        ("code_locations", code_locations),
        ("fix_verification", fix_verification),
        ("fix_pr_body", fix_pr_body),
    ):
        if value:
            report[key] = copy.deepcopy(value)
    if endpoint:
        report["endpoint"] = endpoint
    if method:
        report["method"] = method
    if cve:
        report["cve"] = cve
    if cwe:
        report["cwe"] = cwe

    errors = _validate_rich_fields(report)
    for key in (
        "description",
        "impact",
        "target",
        "technical_analysis",
        "poc_description",
        "poc_script_code",
        "remediation_steps",
        "evidence",
        "counterevidence",
        "confidence",
        "severity_change_conditions",
    ):
        if not str(report.get(key) or "").strip():
            errors.append(f"{key} must be non-empty for a validated dynamic finding")
    if errors:
        return json.dumps({"success": False, "error": "Validation failed", "errors": errors})

    services = ctx.context.services
    model_for = services.model_for if services is not None else None
    # The original dynamic candidate excludes CVE metadata: a matching CVE or
    # title alone never establishes the same endpoint/parameter/root cause.
    candidate = {
        key: report.get(key)
        for key in (
            "title",
            "description",
            "impact",
            "target",
            "technical_analysis",
            "poc_description",
            "poc_script_code",
            "endpoint",
            "method",
        )
    }
    existing = [r for r in run_state.read_reports() if r.get("finding_class", "dynamic") == "dynamic"]
    dedupe = await check_duplicate(
        candidate,
        existing,
        resolve_model=(lambda: model_for("dedupe")) if callable(model_for) else None,
    )
    if dedupe.get("is_duplicate"):
        duplicate_id = str(dedupe.get("duplicate_id") or "")
        duplicate_title = next(
            (r.get("title", "Unknown") for r in existing if r.get("id") == duplicate_id), ""
        )
        message = (
            f"Potential duplicate of '{duplicate_title}' (id={duplicate_id[:8]}...) — "
            "do not re-report the same vulnerability. Use update_vulnerability_report "
            "to revise it with new evidence instead of filing a new report."
        )
        return json.dumps(
            {
                "success": False,
                "is_duplicate": True,
                "duplicate_id": duplicate_id,
                "duplicate_of": duplicate_id,
                "duplicate_title": duplicate_title,
                "confidence": dedupe.get("confidence", 0.0),
                "reason": dedupe.get("reason", ""),
                "error": message,
                "message": message,
            }
        )

    saved = run_state.add_vulnerability_report(
        report, agent_id=ctx.context.agent_id, agent_name=ctx.context.agent_name
    )
    return json.dumps(
        {
            "success": True,
            "message": f"Vulnerability report {saved['id']} filed (severity {saved['severity'].upper()}).",
            "report_id": saved["id"],
            "severity": saved["severity"],
            "cvss_score": score,
        }
    )


# ---------------------------------------------------------------------------
# update_vulnerability_report — revise an existing finding
# ---------------------------------------------------------------------------


@function_tool(strict_mode=False)
async def update_vulnerability_report(
    ctx: RunContextWrapper[EngineContext],
    report_id: str,
    update_reason: str,
    title: str | None = None,
    description: str | None = None,
    impact: str | None = None,
    target: str | None = None,
    technical_analysis: str | None = None,
    poc_description: str | None = None,
    poc_script_code: str | None = None,
    remediation_steps: str | None = None,
    evidence: str | None = None,
    counterevidence: str | None = None,
    confidence: str | None = None,
    severity_change_conditions: str | None = None,
    fix_effort: str | None = None,
    cvss_breakdown: dict[str, str] | None = None,
    endpoint: str | None = None,
    method: str | None = None,
    cve: str | None = None,
    cwe: str | None = None,
    assumptions: str | None = None,
    confidence_rationale: str | None = None,
    code_locations: list[dict[str, Any]] | None = None,
    fix_verification: str | None = None,
    fix_pr_body: str | None = None,
    contextual_cvss_reasoning: str | None = None,
    reachability: str | None = None,
    reachability_evidence: str | None = None,
) -> str:
    """Revise a vulnerability report that is already filed, keeping its id.

    Use this when you learn something a filed finding does not yet carry:
    you built the working exploit after filing, you chained it with another
    finding and the impact is higher, or further testing weakened it and the
    severity must come down. This is NOT deduplication — do not file a
    second report for a finding you can revise.

    Pass only the fields to replace; every other field stays as-is.

    Args:
        report_id: The existing report ID (e.g. "vuln-0001").
        update_reason: Why you are revising (recorded in update_history).
        title: New title (optional).
        description: New description (optional).
        impact: New impact narrative (optional).
        target: New target (optional).
        technical_analysis: New root cause analysis (optional).
        poc_description: New PoC narrative (optional).
        poc_script_code: New Python PoC script (optional).
        remediation_steps: New remediation guidance (optional).
        evidence: New evidence (optional).
        counterevidence: New counterevidence (optional).
        confidence: New confidence assessment (optional).
        severity_change_conditions: New severity conditions (optional).
        fix_effort: New fix effort estimate (optional).
        cvss_breakdown: All 8 canonical CVSS metrics: attack_vector (N/A/L/P),
            attack_complexity (L/H), privileges_required (N/L/H),
            user_interaction (N/R), scope (U/C), confidentiality (N/L/H),
            integrity (N/L/H), availability (N/L/H). Replaces the whole
            vector, score, and severity together (optional).
        endpoint: New endpoint (optional).
        method: New HTTP method (optional).
        cve: New CVE id (optional).
        cwe: New CWE id (optional).
        assumptions: Updated assumptions or uncertainty.
        confidence_rationale: Explanation of confidence and any missing evidence.
        code_locations: Replacement list of relative file/start_line/end_line locations,
            optionally paired fix_before/fix_after blocks. New fixes require fresh verification.
        fix_verification: Verification of security closure and preserved behavior.
        fix_pr_body: Reviewer-facing fix explanation.
        contextual_cvss_reasoning: Dependency findings only: fresh evidence explaining a revised
            cvss_breakdown. Required when changing the contextual rating; preserves advisory_cvss.
        reachability: Dependency findings only: updated usage evidence level.
        reachability_evidence: Fresh evidence for a changed reachability assessment.
    """
    run_state: RunState = ctx.context.run_state  # type: ignore[assignment]

    report = next((r for r in run_state.read_reports() if r.get("id") == report_id), None)
    if report is None:
        return json.dumps({"success": False, "error": f"Report {report_id} not found"})
    if not update_reason.strip():
        return json.dumps({"success": False, "error": "update_reason must be non-empty"})

    # Build the update: only replace non-None fields
    updates: dict = {}
    field_map = {
        "title": title,
        "description": description,
        "impact": impact,
        "target": target,
        "technical_analysis": technical_analysis,
        "poc_description": poc_description,
        "poc_script_code": poc_script_code,
        "remediation_steps": remediation_steps,
        "evidence": evidence,
        "counterevidence": counterevidence,
        "confidence": confidence,
        "assumptions": assumptions,
        "confidence_rationale": (
            confidence_rationale
            if confidence_rationale is not None
            else confidence
            if confidence is not None
            else None
        ),
        "severity_change_conditions": severity_change_conditions,
        "fix_effort": fix_effort,
        "endpoint": endpoint,
        "method": method,
        "cve": cve,
        "cwe": cwe,
        "code_locations": code_locations,
        "fix_verification": fix_verification,
        "fix_pr_body": fix_pr_body,
    }
    for field_name, value in field_map.items():
        if value is not None:
            updates[field_name] = value

    # CVSS recalculation if breakdown provided
    if cvss_breakdown is not None:
        errors = validate_cvss_breakdown(cvss_breakdown)
        if errors:
            return json.dumps({"success": False, "error": "Validation failed", "errors": errors})
        try:
            score, severity, vector = calculate_cvss(cvss_breakdown)
        except ValueError as exc:
            return json.dumps({"success": False, "error": "Validation failed", "errors": [str(exc)]})
        updates.update(
            cvss_breakdown=cvss_breakdown,
            severity=severity,
            cvss=score,
            cvss_vector=vector,
        )

    errors: list[str] = []
    if report.get("finding_class") == "dependency_cve":
        if any(key in updates for key in ("target", "cve")):
            errors.append(
                "Dependency target and CVE identity are immutable; file distinct findings separately"
            )
        metadata = copy.deepcopy(report.get("dependency_metadata") or {})
        if cvss_breakdown is not None:
            if not str(contextual_cvss_reasoning or "").strip():
                errors.append("Dependency rating revisions require fresh contextual_cvss_reasoning")
            else:
                metadata.update(
                    contextual_cvss_breakdown=dict(cvss_breakdown),
                    contextual_cvss_score=updates["cvss"],
                    contextual_cvss_vector=updates["cvss_vector"],
                    contextual_cvss_reasoning=contextual_cvss_reasoning,
                )
        elif contextual_cvss_reasoning is not None:
            if not contextual_cvss_reasoning.strip():
                errors.append("contextual_cvss_reasoning cannot be cleared")
            else:
                metadata["contextual_cvss_reasoning"] = contextual_cvss_reasoning
        if reachability is not None:
            if reachability not in {
                "unknown",
                "not_imported",
                "imported",
                "vulnerable_symbol_used",
                "reachable_call_path",
            }:
                errors.append("reachability is not a supported evidence level")
            if not str(reachability_evidence or "").strip():
                errors.append("A changed reachability requires fresh reachability_evidence")
            metadata["reachability"] = reachability
        if reachability_evidence is not None:
            if not reachability_evidence.strip():
                errors.append("reachability_evidence cannot be cleared")
            metadata["reachability_evidence"] = reachability_evidence
        if metadata != report.get("dependency_metadata"):
            updates["dependency_metadata"] = metadata
        if any(key in updates for key in ("poc_description", "poc_script_code", "code_locations")):
            errors.append("Dependency findings do not accept dynamic PoC or code fix fields")
    elif any(
        key in updates and not str(updates[key]).strip()
        for key in (
            "title",
            "description",
            "impact",
            "target",
            "technical_analysis",
            "poc_description",
            "poc_script_code",
            "remediation_steps",
            "evidence",
            "counterevidence",
            "confidence",
            "severity_change_conditions",
        )
    ):
        errors.append("Required dynamic finding fields cannot be cleared")
    if report.get("finding_class", "dynamic") == "dynamic" and any(
        value is not None for value in (contextual_cvss_reasoning, reachability, reachability_evidence)
    ):
        errors.append("Contextual dependency metadata is only accepted for dependency findings")
    if not updates and not errors:
        return json.dumps({"success": False, "error": "No fields to update"})
    errors.extend(_validate_rich_fields({**report, **updates}))
    if (
        code_locations is not None
        and code_locations != report.get("code_locations")
        and any("fix_after" in loc for loc in code_locations if isinstance(loc, dict))
        and not str(fix_verification or "").strip()
    ):
        errors.append("A revised fix requires fresh fix_verification")
    if errors:
        return json.dumps({"success": False, "error": "Validation failed", "errors": errors})
    changed_fields = list(updates)
    try:
        report = run_state.revise_vulnerability_report(
            report_id,
            updates,
            reason=update_reason,
            agent_id=ctx.context.agent_id,
            agent_name=ctx.context.agent_name,
            validate=_validate_rich_fields,
        )
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)})

    return json.dumps(
        {
            "success": True,
            "message": f"Report {report_id} updated ({', '.join(changed_fields)}).",
            "report_id": report_id,
            "severity": report.get("severity", ""),
            "cvss_score": report.get("cvss"),
        }
    )


@function_tool(strict_mode=False)
def create_dependency_report(
    ctx: RunContextWrapper[EngineContext],
    title: str,
    description: str,
    target: str,
    cve: str,
    package_name: str,
    installed_version: str,
    advisory_cvss: float,
    impact: str,
    remediation_steps: str,
    assumptions: str,
    package_ecosystem: str,
    manifest_path: str,
    technical_analysis: str,
    reachability_evidence: str,
    contextual_cvss_breakdown: dict[str, str],
    contextual_cvss_reasoning: str,
    fixed_version: str = "",
    cwe: str = "",
    fix_effort: str = "low",
    introduced_by: str = "",
    dependency_path: str = "",
    reachability: str = "unknown",
) -> str:
    """File a verified known-CVE dependency finding without inventing a dynamic PoC.

    Verify a published advisory matches the installed version at the exact
    manifest. Cite advisory and installed-version evidence in description and
    technical_analysis. Reachability changes prioritization, not the published
    score. The contextual rating must follow the observed deployment and trace;
    uncertainty is not a reason to invent a lower score. An independently
    reproduced exploit is a separate dynamic report. Duplicates are identified
    by target, CVE, package, ecosystem and manifest, not title alone.

    Args:
        title: Specific CVE/package finding title.
        description: Advisory reference, affected version and observed installed-version proof.
        target: In-scope repository or application.
        cve: Verified CVE-YYYY-NNNN identifier, not a GHSA-only identifier.
        package_name: Exact affected package name.
        installed_version: Version verified in the manifest/lockfile or installed inventory.
        advisory_cvss: Published advisory base score, finite and between 0 and 10.
        impact: Documented consequence in this application context.
        remediation_steps: Upgrade or mitigation instructions.
        assumptions: Uncertainty and unverified conditions.
        package_ecosystem: Normalized ecosystem such as npm, pypi, maven, go or cargo.
        manifest_path: Repository-relative observed manifest or lockfile path.
        technical_analysis: Advisory mechanism and concrete manifest/scanner evidence.
        reachability_evidence: Observed usage and source-to-sink trace, or searches and gaps.
        contextual_cvss_breakdown: All eight canonical CVSS 3.1 metrics, rated from evidence.
        contextual_cvss_reasoning: Explain metrics using observed call sites and trust boundaries;
            retain the advisory reference and explicitly name unknowns.
        fixed_version: Fixed version if published, otherwise empty.
        cwe: Advisory CWE identifier when known.
        fix_effort: trivial/low/moderate/medium/high.
        introduced_by: Direct dependency introducing a transitive package.
        dependency_path: Observed dependency chain.
        reachability: unknown/not_imported/imported/vulnerable_symbol_used/reachable_call_path.
    """
    required = {
        "title": title,
        "description": description,
        "target": target,
        "package_name": package_name,
        "installed_version": installed_version,
        "impact": impact,
        "remediation_steps": remediation_steps,
        "package_ecosystem": package_ecosystem,
        "manifest_path": manifest_path,
        "technical_analysis": technical_analysis,
        "reachability_evidence": reachability_evidence,
        "contextual_cvss_reasoning": contextual_cvss_reasoning,
    }
    errors = [f"{key} must be non-empty" for key, value in required.items() if not value.strip()]
    cve = cve.strip().upper()
    if not re.fullmatch(r"CVE-\d{4}-\d{4,}", cve):
        errors.append("cve must be a verified CVE-YYYY-NNNN identifier")
    if not math.isfinite(advisory_cvss) or not 0 <= advisory_cvss <= 10:
        errors.append("advisory_cvss must be a finite published score between 0 and 10")
    if not _relative_path(manifest_path):
        errors.append("manifest_path must be repository-relative without '.' or '..'")
    ecosystem = package_ecosystem.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]*", ecosystem):
        errors.append("package_ecosystem must be a normalized ecosystem identifier")
    reachability = reachability.strip().lower()
    if reachability not in {
        "unknown",
        "not_imported",
        "imported",
        "vulnerable_symbol_used",
        "reachable_call_path",
    }:
        errors.append("reachability is not a supported evidence level")
    if cwe and not re.fullmatch(r"CWE-\d+", cwe.strip().upper()):
        errors.append("cwe must use CWE-NNN format")
    errors.extend(validate_cvss_breakdown(contextual_cvss_breakdown))
    if errors:
        return json.dumps({"success": False, "error": "Validation failed", "errors": errors})
    score, severity, vector = calculate_cvss(contextual_cvss_breakdown)
    metadata = {
        "package_name": package_name.strip(),
        "installed_version": installed_version.strip(),
        "package_ecosystem": ecosystem,
        "manifest_path": manifest_path,
        "advisory_cvss": advisory_cvss,
        "reachability": reachability,
        "reachability_evidence": reachability_evidence,
        "contextual_cvss_breakdown": dict(contextual_cvss_breakdown),
        "contextual_cvss_score": score,
        "contextual_cvss_vector": vector,
        "contextual_cvss_reasoning": contextual_cvss_reasoning,
    }
    for key, value in (
        ("fixed_version", fixed_version),
        ("introduced_by", introduced_by),
        ("dependency_path", dependency_path),
    ):
        if value.strip():
            metadata[key] = value.strip()
    report: dict[str, Any] = {
        "title": title.strip(),
        "description": description,
        "target": target.strip(),
        "cve": cve,
        "impact": impact,
        "technical_analysis": technical_analysis,
        "remediation_steps": remediation_steps,
        "assumptions": assumptions,
        "fix_effort": fix_effort,
        "finding_class": "dependency_cve",
        "dependency_metadata": metadata,
        "cvss_breakdown": dict(contextual_cvss_breakdown),
        "cvss": score,
        "cvss_vector": vector,
        "severity": severity,
    }
    if cwe:
        report["cwe"] = cwe.strip().upper()
    errors = _validate_rich_fields(report)
    if errors:
        return json.dumps({"success": False, "error": "Validation failed", "errors": errors})
    try:
        saved = ctx.context.run_state.add_vulnerability_report(
            report,
            agent_id=ctx.context.agent_id,
            agent_name=ctx.context.agent_name,
        )
    except DuplicateDependencyReport as exc:
        return json.dumps(
            {
                "success": False,
                "is_duplicate": True,
                "duplicate_id": exc.report_id,
                "error": "This dependency/CVE/manifest already exists; review the existing report.",
            }
        )
    return json.dumps(
        {
            "success": True,
            "report_id": saved["id"],
            "severity": severity,
            "cvss_score": score,
            "finding_class": "dependency_cve",
        }
    )


@function_tool(strict_mode=False)
def list_reports(
    ctx: RunContextWrapper[EngineContext],
    severity: str | None = None,
    finding_class: str | None = None,
    target: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Read a bounded index of this run's shared findings; use get_report for full details.

    Args:
        severity: Optional critical/high/medium/low/info filter.
        finding_class: Optional dynamic/dependency_cve filter.
        target: Optional case-insensitive target substring.
        limit: Page size from 1 to 100.
        offset: Nonnegative pagination offset.
    """
    if not 1 <= limit <= 100 or offset < 0:
        return json.dumps({"success": False, "error": "limit must be 1..100 and offset nonnegative"})
    if severity is not None:
        try:
            severity = normalize_severity(severity)
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc)})
    if finding_class is not None and finding_class not in {"dynamic", "dependency_cve"}:
        return json.dumps({"success": False, "error": "finding_class must be dynamic|dependency_cve"})
    reports = [
        r
        for r in ctx.context.run_state.read_reports()
        if (not severity or r.get("severity") == severity)
        and (not finding_class or r.get("finding_class", "dynamic") == finding_class)
        and (not target or target.casefold() in str(r.get("target", "")).casefold())
    ]
    fields = (
        "id",
        "title",
        "target",
        "endpoint",
        "method",
        "severity",
        "cvss",
        "confidence",
        "finding_class",
        "cve",
        "cwe",
        "agent_id",
        "agent_name",
        "updated_at",
        "dependency_metadata",
    )
    return json.dumps(
        {
            "success": True,
            "total": len(reports),
            "offset": offset,
            "reports": [{k: r[k] for k in fields if k in r} for r in reports[offset : offset + limit]],
        },
        ensure_ascii=False,
    )


@function_tool(strict_mode=False)
def get_report(ctx: RunContextWrapper[EngineContext], report_id: str) -> str:
    """Read one complete finding, including evidence, code fixes and attributed revisions.

    Args:
        report_id: Existing id returned by a reporting tool or list_reports.
    """
    report = next((r for r in ctx.context.run_state.read_reports() if r["id"] == report_id), None)
    if report is None:
        return json.dumps({"success": False, "error": f"Report {report_id} not found"})
    return json.dumps({"success": True, "report": report}, ensure_ascii=False)


@function_tool(strict_mode=False)
def create_finding(
    ctx: RunContextWrapper[EngineContext],
    finding_type: str,
    title: str,
    content: str,
    host: str = "",
    source: str = "",
    severity: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Report a distinct discovery IMMEDIATELY as it happens.

    This is the general-purpose finding channel — for credentials, architecture,
    sensitive data, notable results, and other discoveries that are valuable
    intelligence but not (yet) a validated vulnerability report. Works in both
    web and internal engagements.

    Record each distinct discovery promptly, with observed facts and validation
    status. One extracted dataset may use one finding plus a complete evidence
    attachment; do not emit one finding per row or repeat unchanged facts.

    **Content formatting rules:**
    - Use markdown structure (## sections, bullet points, code blocks)
    - Do NOT write content as a single run-on paragraph
    - For credentials: use a table or bullet list (one item per line)
    - For architecture: use ## per host or service
    - For command output: use ``` code blocks
    - For any multi-part finding: each part gets its own section

    Example structure:
    ```
    ## Summary
    Brief one-line description

    ## Details
    - item 1
    - item 2

    ## Validation
    What was verified vs observed
    ```

    Args:
        finding_type: One of "credential" (accounts/passwords/hashes/tokens),
            "architecture" (topology, services, trusts, pivots),
            "sensitive" (data exposure, secrets, documents),
            "result" (notable command output, other discoveries).
        title: Short identifying title.
        content: Markdown-formatted observation. Use sections, bullets,
            code blocks — never a single run-on paragraph. Reference
            evidence_files for complete raw datasets.
        host: Host the finding relates to, if any.
        source: Where/how it was obtained (tool, technique, path).
        severity: critical/high/medium/low/info, based on demonstrated impact.
        metadata: Structured context, including evidence_files
            (paths relative to /workspace/output/).
    """
    run_state: RunState = ctx.context.run_state  # type: ignore[assignment]
    finding_type = finding_type.strip().lower()
    if finding_type not in ("credential", "architecture", "sensitive", "result"):
        return json.dumps(
            {
                "success": False,
                "errors": [
                    "finding_type must be one of credential|architecture|sensitive|result,"
                    f" got {finding_type!r}"
                ],
            }
        )
    if not title.strip() or not content.strip():
        return json.dumps({"success": False, "errors": ["title and content must be non-empty"]})

    try:
        severity = normalize_severity(severity)
    except ValueError as exc:
        return json.dumps({"success": False, "errors": [str(exc)]})

    finding: dict[str, Any] = {
        "finding_type": finding_type,
        "title": title.strip(),
        "content": content,
        "agent_id": ctx.context.agent_id,
        "agent_name": ctx.context.agent_name,
    }
    if host:
        finding["host"] = host
    if source:
        finding["source"] = source
    if severity:
        finding["severity"] = severity.strip().lower()
    if metadata:
        finding["metadata"] = metadata

    saved = run_state.add_internal_finding(
        finding, agent_id=ctx.context.agent_id, agent_name=ctx.context.agent_name
    )
    return json.dumps(
        {
            "success": True,
            "message": f"Finding {saved['id']} recorded.",
            "finding_id": saved["id"],
        }
    )
