"""Offline evidence/ownership transitions shared by internal agents."""

from __future__ import annotations

import json

import pytest
from agents import RunContextWrapper
from agents.tool_context import ToolContext

from strixops.engine.scanconfig import EngineContext, EngineServices, ScanSpec
from strixops.platform import artifacts
from strixops.platform.events import EventWriter
from strixops.report.state import RunState
from strixops.tools.internal_campaign import get_internal_campaign, record_internal_event


@pytest.fixture
def ctx(tmp_path):
    state = RunState(tmp_path, EventWriter(tmp_path))
    services = EngineServices(spec=ScanSpec("192.0.2.0/24", scan_type="internal"))
    return RunContextWrapper(EngineContext(agent_id="child-a", run_state=state, services=services))


async def invoke(tool, ctx, args):
    raw = json.dumps(args)
    wrapped = ToolContext.from_agent_context(ctx, "campaign-call", tool_name=tool.name, tool_arguments=raw)
    return await tool.on_invoke_tool(wrapped, raw)


async def record(ctx, event_type="artifact_created", **overrides):
    args = {
        "event_type": event_type,
        "host": "192.0.2.10",
        "subject": "/tmp/our-fixture",
        "details": {"evidence": "Fixture creation verified"},
        **overrides,
    }
    return json.loads(await invoke(record_internal_event, ctx, args))


async def inventory(ctx, **args):
    return json.loads(await invoke(get_internal_campaign, ctx, args))


async def test_shared_inventory_persists_and_closes_only_recorded_resource(ctx):
    opened = await record(ctx)
    assert opened["success"]
    resource_id = opened["resource_id"]
    other = RunContextWrapper(
        EngineContext(agent_id="child-b", run_state=ctx.context.run_state, services=ctx.context.services)
    )
    assert (await inventory(other))["open_items"] == [resource_id]
    wrong_host = await record(other, "artifact_removed", resource_id=resource_id, host="192.0.2.11")
    wrong_object = await record(other, "artifact_removed", resource_id=resource_id, subject="/etc/original")
    untracked = await record(other, "artifact_removed", resource_id="not-ours")
    assert all(not r["success"] for r in [wrong_host, wrong_object, untracked])
    assert (await inventory(other))["open_items"] == [resource_id]
    closed = await record(
        other,
        "artifact_removed",
        resource_id=resource_id,
        details={"evidence": "Exact file removal independently checked"},
    )
    assert closed["success"]
    assert (await inventory(ctx))["open_items"] == []
    saved = json.loads((ctx.context.run_state.run_dir / "run.json").read_text())
    assert saved["internal_campaign"]["resources"][resource_id]["state"] == "closed"
    events = [json.loads(line) for line in ctx.context.run_state.events.path.read_text().splitlines()]
    assert len(events) == 2
    assert events[1]["actor"]["agent_id"] == "child-b"
    assert events[1]["payload"]["event_type"] == "artifact_removed"


async def test_modified_original_requires_restore_plan_and_cannot_be_removed(ctx):
    assert not (await record(ctx, "artifact_modified"))["success"]
    opened = await record(
        ctx,
        "artifact_modified",
        details={"evidence": "One fixture setting changed", "restore_plan": "Restore saved fixture value"},
    )
    assert opened["success"]
    rid = opened["resource_id"]
    assert not (await record(ctx, "artifact_removed", resource_id=rid))["success"]
    assert (
        await record(
            ctx,
            "artifact_restored",
            resource_id=rid,
            details={"evidence": "Saved value restored; unrelated entries preserved"},
        )
    )["success"]


async def test_duplicate_retry_does_not_create_another_cleanup_item(ctx):
    first = await record(ctx)
    second = await record(ctx)
    assert first["resource_id"] == second["resource_id"]
    assert second["unchanged"]
    assert ctx.context.run_state.run_record["internal_campaign"]["event_count"] == 1
    assert not (await record(ctx, details={"evidence": "Different untracked change"}))["success"]


async def test_verified_capability_and_retention_need_explicit_evidence(ctx):
    assert not (await record(ctx, "host_capability"))["success"]
    assert not (await record(ctx, "pivot_verified", details={"evidence": "Unreachable", "verified": False}))[
        "success"
    ]
    assert not (await record(ctx, details={"evidence": "Fixture", "persistent": True}))["success"]
    assert (
        await record(
            ctx,
            "host_capability",
            subject="shell",
            details={
                "evidence": "Hostname and identity from session fixture",
                "verified": True,
                "session": "fixture-session",
            },
        )
    )["success"]
    assert (await inventory(ctx, host="192.0.2.11"))["observations"] == []
    assert len((await inventory(ctx, host="192.0.2.10"))["observations"]) == 1
    assert (
        await record(
            ctx,
            details={
                "evidence": "Fixture created",
                "persistent": True,
                "authorization": "Operator fixture instruction explicitly requests retention",
            },
        )
    )["success"]


async def test_web_scan_cannot_write_campaign_inventory(ctx):
    ctx.context.services.spec.scan_type = "web"
    assert not (await record(ctx))["success"]
    assert not (await inventory(ctx))["success"]
    assert "internal_campaign" not in ctx.context.run_state.run_record


async def test_failed_persistence_does_not_report_duplicate_success_on_retry(ctx, monkeypatch):
    original = artifacts.write_run_record

    def fail(*args, **kwargs):
        raise OSError("Synthetic unavailable storage")

    monkeypatch.setattr(artifacts, "write_run_record", fail)
    raw = await invoke(
        record_internal_event,
        ctx,
        {
            "event_type": "artifact_created",
            "host": "192.0.2.10",
            "subject": "/tmp/fixture",
            "details": {"evidence": "Fixture"},
        },
    )
    assert "error" in raw.lower()
    assert "internal_campaign" not in ctx.context.run_state.run_record
    monkeypatch.setattr(artifacts, "write_run_record", original)
    assert (await record(ctx))["success"]


async def test_later_retention_keeps_original_restore_plan_and_remains_open(ctx):
    opened = await record(
        ctx,
        "artifact_modified",
        details={"evidence": "Fixture modified", "restore_plan": "Restore baseline fixture"},
    )
    rid = opened["resource_id"]
    assert not (
        await record(
            ctx,
            "resource_retained",
            resource_id=rid,
            details={"evidence": "Request received", "persistent": True},
        )
    )["success"]
    assert (
        await record(
            ctx,
            "resource_retained",
            resource_id=rid,
            details={
                "evidence": "Operator requested retention",
                "persistent": True,
                "authorization": "Explicit test-fixture operator instruction",
            },
        )
    )["success"]
    snapshot = await inventory(ctx)
    assert snapshot["open_items"] == snapshot["retained_items"] == [rid]
    assert snapshot["resources"][0]["details"]["restore_plan"] == "Restore baseline fixture"
    assert (
        await record(
            ctx,
            "artifact_restored",
            resource_id=rid,
            details={"evidence": "Baseline restored after retention expired"},
        )
    )["success"]
    assert (await inventory(ctx))["retained_items"] == []
