"""Only authorized, successful lifecycle calls may retire an agent."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from agents.tool_context import ToolContext

from strixops.engine.coordinator import AgentCoordinator
from strixops.engine.scanconfig import EngineContext, EngineServices
from strixops.platform.events import EventWriter
from strixops.tools.lifecycle import agent_finish, finish_scan

FINISH_FIELDS = {
    "executive_summary": "Local fixture complete",
    "methodology": "In-memory observations",
    "technical_analysis": "No external systems accessed",
    "recommendations": "None",
}


async def _invoke(tool, context, arguments):
    raw = json.dumps(arguments)
    tool_context = ToolContext(
        context=context, tool_name=tool.name, tool_call_id="lifecycle-fixture", tool_arguments=raw
    )
    return await tool.on_invoke_tool(tool_context, raw)


@pytest.fixture()
async def environment(tmp_path):
    coordinator = AgentCoordinator(tmp_path, EventWriter(tmp_path))
    await coordinator.register("root", "root", parent_id=None, task="Local fixture")
    run_state = Mock()
    root = EngineContext(run_state=run_state, services=EngineServices(coordinator=coordinator))
    return root, coordinator, run_state


async def test_finish_scan_records_completion_only_after_report_fields_are_saved(environment):
    root, _, run_state = environment

    def update(**kwargs):
        assert root.lifecycle_completion is None
        assert kwargs["executive_summary"] == FINISH_FIELDS["executive_summary"]

    run_state.update_final_fields.side_effect = update
    payload = json.loads(await _invoke(finish_scan, root, FINISH_FIELDS))
    assert payload["success"] is True and payload["scan_completed"] is True
    assert root.lifecycle_completion.tool_name == "finish_scan"
    assert root.lifecycle_completion.payload == payload
    run_state.update_final_fields.assert_called_once()


async def test_active_children_block_report_publication_and_completion(environment):
    root, coordinator, run_state = environment
    await coordinator.register("child", "child", parent_id="root", task="Still working")
    payload = json.loads(await _invoke(finish_scan, root, FINISH_FIELDS))
    assert payload["success"] is False and "Child agents are still working" in payload["message"]
    assert root.lifecycle_completion is None
    run_state.update_final_fields.assert_not_called()


@pytest.mark.parametrize("parent_id", [None, "root"])
async def test_wrong_role_cannot_publish_a_report_or_retire(environment, parent_id):
    root, _, run_state = environment
    root.parent_id = parent_id
    tool, arguments = (
        (agent_finish, {"result_summary": "Invalid root retirement"})
        if parent_id is None else (finish_scan, FINISH_FIELDS)
    )
    payload = json.loads(await _invoke(tool, root, arguments))
    assert payload["success"] is False
    assert root.lifecycle_completion is None
    run_state.update_final_fields.assert_not_called()


async def test_report_write_error_does_not_record_completion(environment):
    root, _, run_state = environment
    run_state.update_final_fields.side_effect = OSError("fixture report write failed")
    result = await _invoke(finish_scan, root, FINISH_FIELDS)
    assert "fixture report write failed" in result
    assert root.lifecycle_completion is None


async def test_child_records_completion_and_delivers_report_even_for_blocked_assignment(environment):
    root, coordinator, _ = environment
    await coordinator.register("child", "child", parent_id="root", task="Local fixture")
    child = EngineContext(agent_id="child", parent_id="root", services=root.services)
    payload = json.loads(await _invoke(
        agent_finish, child,
        {"result_summary": "Assignment blocked", "success": False, "open_items": "Missing access"},
    ))
    assert payload["success"] is True and payload["agent_finished"] is True
    assert payload["completion_report"]["success"] is False
    assert child.lifecycle_completion.tool_name == "agent_finish"
    assert child.lifecycle_completion.payload == payload
    assert "Assignment blocked" in coordinator.pending_messages("root")[0]["content"]
    assert root.lifecycle_completion is None


@pytest.mark.parametrize("value, expected", [(" Critical ", "critical"), ("Informational", "info"), ("", "")])
async def test_overall_severity_is_normalized_before_report_publication(environment, value, expected):
    root, _, run_state = environment
    payload = json.loads(await _invoke(finish_scan, root, {**FINISH_FIELDS, "overall_severity": value}))
    assert payload["success"] is True
    assert run_state.update_final_fields.call_args.kwargs["overall_severity"] == expected


async def test_invalid_overall_severity_does_not_publish_or_complete(environment):
    root, _, run_state = environment
    payload = json.loads(await _invoke(finish_scan, root, {**FINISH_FIELDS, "overall_severity": "urgent"}))
    assert payload["success"] is False and "severity must be" in payload["message"]
    run_state.update_final_fields.assert_not_called()
    assert root.lifecycle_completion is None
