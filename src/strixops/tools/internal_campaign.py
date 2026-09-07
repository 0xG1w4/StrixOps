"""Shared, durable internal observations and cleanup inventory.

These tools record facts; they never execute commands or grant authorization.
Async entrypoints deliberately do not yield during the read/modify/save step,
so agents on the shared event loop cannot interleave ledger transitions.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any, Literal

from agents import RunContextWrapper, function_tool

from strixops.engine.scanconfig import EngineContext
from strixops.platform import artifacts

EventType = Literal[
    "artifact_created",
    "artifact_modified",
    "artifact_removed",
    "artifact_restored",
    "account_created",
    "account_removed",
    "resource_retained",
    "host_capability",
    "egress_observed",
    "defense_observed",
    "pivot_verified",
]
_OPEN_EVENTS = {"artifact_created", "artifact_modified", "account_created"}
_CLOSE_EVENTS = {
    "artifact_removed": "artifact_created",
    "artifact_restored": "artifact_modified",
    "account_removed": "account_created",
}
_OBSERVATIONS = {"host_capability", "egress_observed", "defense_observed", "pivot_verified"}


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message})


def _run_state(context: EngineContext) -> Any:
    state = context.run_state
    spec = getattr(context.services, "spec", None)
    scan_type = getattr(spec, "scan_type", None) or (
        state.run_record.get("scan_config", {}).get("scan_type") if state is not None else None
    )
    if state is None or scan_type != "internal":
        raise ValueError("Internal campaign tools require an internal scan with run state")
    return state


@function_tool(strict_mode=False)
async def record_internal_event(
    ctx: RunContextWrapper[EngineContext],
    event_type: EventType,
    host: str,
    subject: str,
    details: dict[str, Any] | None = None,
    resource_id: str = "",
) -> str:
    """Record an observed capability or an engagement-created change.

    This updates the shared campaign inventory in run.json and emits an audit
    event. It does not perform cleanup or verify claims on the remote host.
    Read get_internal_campaign before cleanup or handing work to another agent.

    Args:
        event_type: artifact_created/modified opens a cleanup item; removed
            closes a created artifact, restored closes a modified artifact.
            account_created/removed tracks only accounts we created.
            resource_retained records subsequent explicit permission to leave
            an existing open resource; it does not mark that resource cleaned.
            host_capability, egress_observed, defense_observed and pivot_verified
            record current observations without implying access to other hosts.
        host: Exact host where the observation or change occurred.
        subject: Exact path, account name, or capability/route identifier.
        details: Evidence and context. Every event needs an evidence string.
            artifact_modified also needs a restore_plan identifying the saved
            original state. Persistent changes need persistent=true and an
            authorization string referring to explicit operator permission.
            host_capability needs verified=true/false; pivot_verified needs
            verified=true. Record session and identity here when applicable.
        resource_id: ID returned by a created/modified event; required when
            recording removal/restoration/retention. Closing requires the same host and
            subject, so an unrelated original file cannot be marked cleaned.
    """
    try:
        state = _run_state(ctx.context)
    except ValueError as exc:
        return _error(str(exc))
    event_type, host, subject = event_type.strip(), host.strip(), subject.strip()
    details = dict(details or {})
    if event_type not in _OPEN_EVENTS | _CLOSE_EVENTS.keys() | _OBSERVATIONS | {"resource_retained"}:
        return _error("Unknown internal event type")
    if not host or not subject:
        return _error("host and subject must be non-empty")
    if not isinstance(details.get("evidence"), str) or not details["evidence"].strip():
        return _error("details.evidence must describe the observed result")
    if "persistent" in details and not isinstance(details["persistent"], bool):
        return _error("details.persistent must be a boolean")
    if details.get("persistent") and not str(details.get("authorization") or "").strip():
        return _error("Persistent changes require an explicit operator authorization reference")
    if event_type == "artifact_modified" and not str(details.get("restore_plan") or "").strip():
        return _error("Modified artifacts require details.restore_plan for the original state")
    if event_type == "host_capability" and not isinstance(details.get("verified"), bool):
        return _error("host_capability requires details.verified=true/false")
    if event_type == "pivot_verified" and details.get("verified") is not True:
        return _error("pivot_verified requires details.verified=true after a successful route check")
    if event_type == "resource_retained" and details.get("persistent") is not True:
        return _error("resource_retained requires details.persistent=true and explicit authorization")

    campaign = copy.deepcopy(
        state.run_record.get("internal_campaign")
        or {
            "resources": {},
            "observations": {},
            "event_count": 0,
        }
    )
    resources = campaign["resources"]
    now = artifacts.utc_stamp()
    if event_type in _OPEN_EVENTS:
        if resource_id:
            return _error("New changes must omit resource_id; use the returned ID for cleanup")
        for existing_id, resource in resources.items():
            if resource["host"] == host and resource["subject"] == subject and resource["state"] == "open":
                if resource["opened_by"] == event_type and resource["details"] == details:
                    return json.dumps({"success": True, "resource_id": existing_id, "unchanged": True})
                return _error("This host/subject already has an open cleanup item; inspect the inventory")
        resource_id = uuid.uuid4().hex[:12]
        resources[resource_id] = {
            "resource_id": resource_id,
            "host": host,
            "subject": subject,
            "opened_by": event_type,
            "state": "open",
            "details": details,
            "created_at": now,
            "agent_id": ctx.context.agent_id,
        }
    elif event_type == "resource_retained":
        resource = resources.get(resource_id)
        if resource is None or resource["state"] != "open":
            return _error("Retention requires an existing open resource_id")
        if (resource["host"], resource["subject"]) != (host, subject):
            return _error("Retention must match the recorded host and subject")
        resource["details"].update(persistent=True, authorization=details["authorization"])
        resource["retention_evidence"] = details["evidence"]
        resource["retained_at"] = now
    elif event_type in _CLOSE_EVENTS:
        resource = resources.get(resource_id)
        if resource is None:
            return _error("Cleanup requires an existing resource_id from this engagement")
        if (resource["host"], resource["subject"], resource["opened_by"]) != (
            host,
            subject,
            _CLOSE_EVENTS[event_type],
        ):
            return _error("Cleanup event does not match the recorded host, subject and change type")
        if resource["state"] == "closed":
            return json.dumps({"success": True, "resource_id": resource_id, "unchanged": True})
        resource.update(
            state="closed",
            closed_at=now,
            closed_by=ctx.context.agent_id,
            cleanup_evidence=details["evidence"],
        )
    else:
        if resource_id:
            return _error("Observations must omit resource_id")
        key = json.dumps([host, event_type, subject], ensure_ascii=False)
        campaign["observations"][key] = {
            "host": host,
            "event_type": event_type,
            "subject": subject,
            "details": details,
            "observed_at": now,
            "agent_id": ctx.context.agent_id,
        }

    campaign["event_count"] += 1
    updated_record = dict(state.run_record, internal_campaign=campaign)
    artifacts.write_run_record(state.run_dir, updated_record)
    state.run_record["internal_campaign"] = campaign
    state.events.emit(
        event_type="campaign.internal_event",
        payload={
            "event_type": event_type,
            "host": host,
            "subject": subject,
            "resource_id": resource_id,
            "details": details,
        },
        agent_id=ctx.context.agent_id,
        agent_name=ctx.context.agent_name,
    )
    return json.dumps({"success": True, "resource_id": resource_id, "recorded": event_type})


@function_tool(strict_mode=False)
async def get_internal_campaign(ctx: RunContextWrapper[EngineContext], host: str = "") -> str:
    """Read shared host observations and cleanup inventory, including open items.

    Args:
        host: Optional exact host filter; empty returns the whole engagement.
    """
    try:
        state = _run_state(ctx.context)
    except ValueError as exc:
        return _error(str(exc))
    campaign = state.run_record.get("internal_campaign") or {}
    host = host.strip()
    resources = [r for r in campaign.get("resources", {}).values() if not host or r["host"] == host]
    observations = [r for r in campaign.get("observations", {}).values() if not host or r["host"] == host]
    return json.dumps(
        {
            "success": True,
            "resources": resources,
            "observations": observations,
            "open_items": [r["resource_id"] for r in resources if r["state"] == "open"],
            "retained_items": [
                r["resource_id"] for r in resources if r["state"] == "open" and r["details"].get("persistent")
            ],
            "note": "Agent-supplied observations are not independent runtime verification.",
        },
        ensure_ascii=False,
    )
