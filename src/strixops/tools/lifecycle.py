"""Lifecycle tools: ``finish_scan`` (root) and ``agent_finish`` (child)."""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from strixops.engine.scanconfig import EngineContext, LifecycleCompletion
from strixops.report.state import RunState, normalize_severity


@function_tool(strict_mode=False)
def finish_scan(
    ctx: RunContextWrapper[EngineContext],
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
) -> str:
    """End the penetration test and publish the executive report.

    Call this exactly once, when testing is complete and every finding has
    been filed. This is the ONLY way the scan terminates successfully. The
    report is the client-facing deliverable: write every field richly and
    concretely in the scan's report language (default 简体中文), clustering
    related findings into themes rather than listing them one by one. Do NOT
    include remediation advice in the narrative fields (recommendations is
    the only remediation field).

    Severity must be canonical and scored for THIS engagement:
      Critical for confirmed RCE, domain-wide administration, root access
      or equivalent control of a critical system, or verified exposure
      granting full access to critical data · High for verified privileged
      or service-account access, sensitive data exposure, or significant
      limited compromise ·
      Medium for meaningful but limited-impact weakness · Low for minor
      exposure · Info for observational findings.

    Args:
        executive_summary: 执行摘要 — what was tested, what was found, overall
            risk. Lead with the most severe issues.
        methodology: How the assessment was conducted: phases, tooling, and
            coverage. Be concrete about what was and was not tested.
        technical_analysis: Deeper analysis of the findings and attack chains
            for a technical audience.
        recommendations: Prioritized remediation guidance, most urgent first.
        overall_severity: One of Critical|High|Medium|Low|Info for the whole
            engagement.
        severity_rationale: 1-3 concise sentences justifying the severity.
        battle_gains: 本阶段战果整理 — the concrete access, data and footholds
            gained (not generic observations).
        attack_narrative: 攻击路径与关键进展 — how the attack chain unfolded,
            entry → pivot → objective.
        environment_map: 内网架构、关键主机与服务 — hosts, services, access
            paths and what each system appears to do.
        credential_capabilities: 凭证、哈希与访问能力 — accounts, hashes,
            keys and the access they grant.
        future_leverage: 后续可利用路径 — what can be exploited next from the
            access already obtained.
        business_impact: 敏感数据与业务冲击 — sensitive data reached and the
            business consequences.
        limitations: 本阶段限制与未完成部分 — coverage gaps, blocked paths,
            partial execution.
    """
    if ctx.context.parent_id is not None:
        return json.dumps({"success": False, "message": "Only the root agent may call finish_scan."})

    try:
        overall_severity = normalize_severity(overall_severity)
    except ValueError as exc:
        return json.dumps({"success": False, "message": str(exc)})

    run_state: RunState = ctx.context.run_state  # type: ignore[assignment]
    coordinator = getattr(ctx.context.services, "coordinator", None)

    active = coordinator.active_ids(exclude=ctx.context.agent_id) if coordinator is not None else []
    if active:
        return json.dumps(
            {
                "success": False,
                "message": (
                    "Child agents are still working "
                    f"({len(active)} active). Wait for them via wait_for_agents, "
                    "or stop them with stop_agent, before ending the scan."
                ),
            }
        )

    if coordinator is not None and not coordinator.begin_finish(ctx.context.agent_id):
        return json.dumps(
            {"success": False, "message": "Agent completion is already pending or the scan is stopping."}
        )

    try:
        run_state.update_final_fields(
            executive_summary=executive_summary,
            methodology=methodology,
            technical_analysis=technical_analysis,
            recommendations=recommendations,
            overall_severity=overall_severity.strip(),
            severity_rationale=severity_rationale.strip(),
            battle_gains=battle_gains.strip(),
            attack_narrative=attack_narrative.strip(),
            environment_map=environment_map.strip(),
            credential_capabilities=credential_capabilities.strip(),
            future_leverage=future_leverage.strip(),
            business_impact=business_impact.strip(),
            limitations=limitations.strip(),
        )
    except BaseException:
        if coordinator is not None:
            coordinator.abort_finish(ctx.context.agent_id)
        raise
    payload = {
        "success": True,
        "message": "Scan complete. Executive report published.",
        "scan_completed": True,
    }
    ctx.context.lifecycle_completion = LifecycleCompletion("finish_scan", payload)
    return json.dumps(payload)


@function_tool(strict_mode=False)
async def agent_finish(
    ctx: RunContextWrapper[EngineContext],
    result_summary: str,
    findings: str = "",
    open_items: str | list[str] = "",
    success: bool = True,
    report_to_parent: bool = True,
) -> str:
    """Finish your assigned task and retire as a child agent.

    Args:
        result_summary: What you were asked to do, what you did, and the
            outcome. This is the report your parent agent receives.
        findings: Concrete findings discovered (file any formal reports first
            with create_vulnerability_report / create_internal_finding).
        open_items: Untested or uncertain work, as text or a list of follow-up items.
        success: Whether you completed your assignment.
        report_to_parent: Deliver the completion report to your parent agent.
    """
    if ctx.context.parent_id is None:
        return json.dumps({"success": False, "message": "Only child agents may call agent_finish."})

    coordinator = getattr(ctx.context.services, "coordinator", None)
    if coordinator is not None:
        outstanding = coordinator.active_descendants(ctx.context.agent_id)
        if outstanding:
            return json.dumps(
                {
                    "success": False,
                    "error_code": "ACTIVE_DESCENDANTS",
                    "message": (
                        "Descendants are still working or settling; "
                        "wait for them or stop them before finishing."
                    ),
                    "active_agent_ids": outstanding,
                }
            )
        if not coordinator.begin_finish(ctx.context.agent_id):
            return json.dumps(
                {"success": False, "message": "Agent completion is already pending or the scan is stopping."}
            )
    try:
        if report_to_parent and ctx.context.parent_id and ctx.context.services is not None:
            report_lines = [
                f"== Completion report from {ctx.context.agent_name} ({ctx.context.agent_id}) ==",
                f"Success: {success}",
                f"Result: {result_summary}",
            ]
            if findings:
                report_lines.append(f"Findings: {findings}")
            if open_items:
                report_lines.append(
                    "Open items: " + ("; ".join(open_items) if isinstance(open_items, list) else open_items)
                )
            await coordinator.send(
                ctx.context.parent_id,
                {
                    "from": ctx.context.agent_id,
                    "from_name": ctx.context.agent_name,
                    "type": "completion_report",
                    "priority": "normal",
                    "content": "\n".join(report_lines),
                },
            )
    except BaseException:
        if coordinator is not None:
            coordinator.abort_finish(ctx.context.agent_id)
        raise
    payload = {
        "success": True,
        "message": "Task complete; agent retiring.",
        "agent_finished": True,
        "completion_report": {
            "result_summary": result_summary,
            "findings": findings,
            "open_items": open_items,
            "success": success,
            "report_to_parent": report_to_parent,
        },
    }
    ctx.context.lifecycle_completion = LifecycleCompletion("agent_finish", payload)
    return json.dumps(payload)


def lifecycle_tool_names() -> list[str]:
    return ["finish_scan", "agent_finish"]
