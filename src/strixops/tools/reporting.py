"""Reporting tools: vulnerability reports (web) and internal findings."""

from __future__ import annotations

import json
from typing import Any

from agents import RunContextWrapper, function_tool

from strixops.engine.scanconfig import EngineContext
from strixops.platform import artifacts as artifacts_mod
from strixops.report.cvss import calculate_cvss, validate_cvss_breakdown
from strixops.report.dedupe import check_duplicate
from strixops.report.state import RunState, normalize_severity


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
        "confidence_rationale": confidence,
        "severity_change_conditions": severity_change_conditions,
        "fix_effort": fix_effort,
        "severity": severity,
        "cvss_breakdown": cvss_breakdown,
        "cvss": score,
        "cvss_vector": vector,
    }
    if endpoint:
        report["endpoint"] = endpoint
    if method:
        report["method"] = method
    if cve:
        report["cve"] = cve
    if cwe:
        report["cwe"] = cwe

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
    existing = list(run_state.reports)
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
    """
    run_state: RunState = ctx.context.run_state  # type: ignore[assignment]

    report = next((r for r in run_state.reports if r.get("id") == report_id), None)
    if report is None:
        return json.dumps({"success": False, "error": f"Report {report_id} not found"})

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
        "confidence_rationale": confidence,
        "severity_change_conditions": severity_change_conditions,
        "fix_effort": fix_effort,
        "endpoint": endpoint,
        "method": method,
        "cve": cve,
        "cwe": cwe,
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

    if not updates:
        return json.dumps({"success": False, "error": "No fields to update"})

    # Record update history
    history = report.get("update_history", [])
    changed_fields = list(updates.keys())
    old_values = {k: report.get(k) for k in changed_fields}
    history.append(
        {
            "reason": update_reason,
            "fields": changed_fields,
            "dropped_fields": [],
            "agent_id": ctx.context.agent_id,
            "previous": old_values,
        }
    )
    updates["update_history"] = history
    updates["updated_at"] = artifacts_mod.utc_stamp()

    # Apply
    report.update(updates)
    run_state.save()

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
