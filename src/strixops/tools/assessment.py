"""Shared coverage and threat-model tools for the normal StrixOps agent tree."""

from __future__ import annotations

import json
from typing import Any

from agents import RunContextWrapper, function_tool

from strixops.engine.scanconfig import EngineContext


def _invoke(
    ctx: RunContextWrapper[EngineContext], operation: str, *, author: bool = False, **kwargs: Any
) -> str:
    if author:
        kwargs.update(agent_id=ctx.context.agent_id, agent_name=ctx.context.agent_name)
    try:
        result = getattr(ctx.context.run_state.assessment, operation)(**kwargs)
    except ValueError as exc:
        result = {"success": False, "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


@function_tool(strict_mode=False)
def record_coverage(
    ctx: RunContextWrapper[EngineContext], surface: str, risk_area: str, outcome: str, evidence: str = ""
) -> str:
    """Record a reviewed surface/risk in the shared run ledger as testing proceeds.

    Outcomes: reported (filed issue), no_issue_found (tested with no issue),
    ruled_out (a specific candidate disproved), not_applicable (risk cannot
    apply), needs_follow_up (unresolved). Missing information is not evidence
    of safety: use needs_follow_up. Evidence is required for ruled_out,
    not_applicable and needs_follow_up; name the control, reason or gap.
    Duplicate surface/risk pairs return the existing id; use update_coverage.
    Keep planned work in the shared assessment-plan note. Pending, blocked or
    untested work has no safe/clean outcome; record a concrete unresolved gap
    as needs_follow_up. A discovery awaiting independent validation remains
    needs_follow_up; reported requires a successfully filed report.
    Include the HTTP method, role/tenant and relevant workflow state in surface
    when they identify different tests. Describe the tested combinations and
    remaining gaps in evidence; one successful role/state test does not clear
    the others. Reuse the plan's identities so results can be reconciled.

    Args:
        surface: Endpoint, file, component or host, with method/role/state when relevant.
        risk_area: Risk assessed, such as SQL injection or authorization.
        outcome: One of reported/no_issue_found/ruled_out/not_applicable/needs_follow_up.
        evidence: Test performed, decisive control, reason or unresolved gap.
    """
    return _invoke(
        ctx,
        "record_coverage",
        author=True,
        surface=surface,
        risk_area=risk_area,
        outcome=outcome,
        evidence=evidence,
    )


@function_tool(strict_mode=False)
def update_coverage(
    ctx: RunContextWrapper[EngineContext], entry_id: str, outcome: str, evidence: str = ""
) -> str:
    """Revise an existing review, retaining its previous conclusion and author.

    Use this to resolve another agent's needs_follow_up, or reopen a conclusion
    that later evidence disproves. Surface/risk identity stays fixed. Never
    turn uncertainty into no_issue_found or ruled_out without testing.
    Read the current row first. Preserve still-relevant tested combinations and
    unresolved gaps in the replacement evidence; do not erase another role's
    limitation merely because your own test succeeded. For a distinct role or
    workflow state, record a separately identified surface instead.

    Args:
        entry_id: Existing id returned by record_coverage or list_coverage.
        outcome: reported/no_issue_found/ruled_out/not_applicable/needs_follow_up.
        evidence: What changed; required for ruled_out/not_applicable/needs_follow_up.
    """
    return _invoke(ctx, "update_coverage", author=True, entry_id=entry_id, outcome=outcome, evidence=evidence)


@function_tool(strict_mode=False)
def list_coverage(
    ctx: RunContextWrapper[EngineContext], outcome: str | None = None, surface: str | None = None
) -> str:
    """Read the shared ledger and run-wide outcome counts before finalizing.

    Compare these records with the shared assessment plan and child handoffs,
    including planned work that has no row yet. Reconcile needs_follow_up rows.
    Report any remaining gaps honestly;
    finishing execution does not resolve them or prove complete coverage.

    Args:
        outcome: Optional exact outcome filter.
        surface: Optional case-insensitive substring filter.
    """
    return _invoke(ctx, "list_coverage", outcome=outcome, surface=surface)


@function_tool(strict_mode=False)
def save_threat_model(ctx: RunContextWrapper[EngineContext], target: str, content: str) -> str:
    """Share the baseline threat model for this target and run.

    Describe the real system, actors, trust boundaries, attacker-controlled
    inputs, exposed surfaces and context-specific severity. Distinguish
    observations from assumptions and unknowns. Use the scan's target value
    so other agents can find the same model. The root, or one mapper explicitly
    designated by the root, owns baseline creation. Other children read and
    amend it rather than independently replacing it. This replaces the active body
    and clears active amendments; previous versions remain in audit history.
    Before replacing a baseline, its owner must read and incorporate the current
    amendments. Use amend_threat_model for incremental corrections. No later run
    inherits it.

    Args:
        target: Target URL, host or repository path.
        content: Markdown overview, boundaries, attack surface and severity calibration.
    """
    return _invoke(ctx, "save_threat_model", author=True, target=target, content=content)


@function_tool(strict_mode=False)
def get_threat_model(ctx: RunContextWrapper[EngineContext], target: str) -> str:
    """Read the target's shared baseline and all active attributed amendments.

    Read corrections as part of the model; baseline assumptions may be wrong.
    If found is false, the root or its designated mapper creates a baseline
    from available evidence and marks unknowns. Other children notify the root
    and continue bounded assigned reconnaissance or independent work that does
    not require the missing assumptions. Save observations in shared notes and
    amend the model after its owner creates it. Do not wait on each other,
    invent a baseline, or probe beyond the verified scope to fill the gap.

    Args:
        target: Exact URL, host or repository path used when saving the model.
    """
    return _invoke(ctx, "get_threat_model", target=target)


@function_tool(strict_mode=False)
def amend_threat_model(ctx: RunContextWrapper[EngineContext], target: str, addendum: str) -> str:
    """Append an attributed correction without overwriting another agent's work.

    Name the earlier assumption, corrected observation, and supporting endpoint,
    file or evidence. Amendments are visible to every agent in this run.

    Args:
        target: Target value used by save_threat_model.
        addendum: Markdown correction or additional boundary/surface evidence.
    """
    return _invoke(ctx, "amend_threat_model", author=True, target=target, addendum=addendum)
