"""Reference child history snapshots through real SDK tools and isolated sessions."""

from __future__ import annotations

import asyncio
import copy
import json
from collections import deque
from typing import Any
from unittest.mock import Mock

import pytest
from agents import Agent, Model, ModelResponse, StopAtTools, function_tool
from agents.items import Usage
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from strixops.config.context import ContextSettings
from strixops.engine.coordinator import AgentCoordinator
from strixops.engine.loop import run_agent_loop
from strixops.engine.scanconfig import EngineContext, EngineServices, ScanSpec
from strixops.engine.sessions import open_agent_session, scrub_images_from_items
from strixops.engine.spawn import _child_initial_input, make_spawn_child
from strixops.platform.events import EventWriter
from strixops.testing.scripted_gateway import completed_stream_event
from strixops.tools.collaboration import create_agent
from strixops.tools.lifecycle import finish_scan

BACKGROUND_START = "== Inherited context from parent (background only) ==\n"
BACKGROUND_END = "\n== End of inherited context =="


def inherited_history(content: str) -> list[Any]:
    rendered = content.split(BACKGROUND_START, 1)[1].split(BACKGROUND_END, 1)[0]
    return json.loads(rendered)


@pytest.mark.parametrize("inherit_context", [None, True, False])
async def test_create_agent_snapshots_tool_context_without_root_input_fallback(inherit_context):
    spawner = Mock(return_value={"ok": True, "name": "validator", "agent_id": "child"})
    services = EngineServices(spawn_child=spawner, root_input="INITIAL_CAMPAIGN_ONLY")
    parent = EngineContext(agent_id="parent", agent_name="parent", services=services)
    arguments = {"name": " validator ", "task": "Validate observed behavior", "skills": ["tooling/python"]}
    if inherit_context is not None:
        arguments["inherit_context"] = inherit_context
    raw = json.dumps(arguments)
    context = ToolContext(
        context=parent,
        tool_name="create_agent",
        tool_call_id="spawn-call",
        tool_arguments=raw,
        turn_input=[{"role": "assistant", "content": "ALREADY_OBSERVED_FINDING"}],
    )
    original = copy.deepcopy(context.turn_input)
    result = json.loads(await create_agent.on_invoke_tool(context, raw))
    assert result["success"] and result["agent_id"] == "child"
    called = spawner.call_args.kwargs
    assert called["parent"] is parent
    assert called["name"] == "validator"
    assert called["skills"] == ["tooling/python"]
    assert called["parent_history"] == ([] if inherit_context is False else original)
    assert called["parent_history"] is not context.turn_input
    called["parent_history"].append({"role": "user", "content": "child-local item"})
    assert context.turn_input == original


async def test_empty_turn_history_does_not_fall_back_to_initial_campaign():
    spawner = Mock(return_value={"ok": True, "name": "validator", "agent_id": "child"})
    services = EngineServices(spawn_child=spawner, root_input="INITIAL_CAMPAIGN_ONLY")
    raw = json.dumps({"name": "validator", "task": "Fresh assignment"})
    context = ToolContext(
        context=EngineContext(services=services),
        tool_name="create_agent",
        tool_call_id="empty-spawn",
        tool_arguments=raw,
    )
    assert json.loads(await create_agent.on_invoke_tool(context, raw))["success"]
    assert spawner.call_args.kwargs["parent_history"] == []


def test_child_background_is_scrubbed_json_before_own_identity_and_assignment():
    history = [
        {"role": "user", "content": "已確認的登入方式與端點"},
        {
            "type": "function_call_output",
            "call_id": "screenshot-call",
            "output": [
                {"type": "input_text", "text": "Login form visible at /login"},
                {"type": "input_image", "image_url": "data:image/png;base64,PARENT_IMAGE"},
            ],
        },
    ]
    original = copy.deepcopy(history)
    services = EngineServices(root_input="INITIAL_CAMPAIGN_ONLY", spec=ScanSpec("https://scope.invalid"))
    parent = EngineContext(agent_id="parent-id", agent_name="parent agent")
    messages = _child_initial_input(services, "validator", "child-id", parent, "CHILD_ASSIGNMENT", history)
    assert len(messages) == 1 and messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert inherited_history(content) == scrub_images_from_items(history)
    assert "[screenshot omitted from inherited context]" in content
    assert "已確認的登入方式與端點" in content
    assert "data:image" not in content and "PARENT_IMAGE" not in content
    assert "INITIAL_CAMPAIGN_ONLY" not in content
    assert "do not continue the parent's work" in content
    assert content.index(BACKGROUND_START) < content.index("You are agent validator (child-id)")
    assert content.index("your parent is parent-id") < content.index("CHILD_ASSIGNMENT")
    assert "Maintain your own identity" in content and "agent_finish" in content
    assert history == original
    history[0]["content"] = "PARENT_LATER_CHANGE"
    assert "PARENT_LATER_CHANGE" not in content


@pytest.mark.parametrize("scan_type", ["web", "internal"])
def test_clean_child_retains_assignment_and_existing_completion_tools(scan_type):
    services = EngineServices(
        root_input="INITIAL_CAMPAIGN_ONLY", spec=ScanSpec("https://scope.invalid", scan_type=scan_type)
    )
    messages = _child_initial_input(
        services, "validator", "child-id", EngineContext(), "CHILD_ASSIGNMENT", []
    )
    assert len(messages) == 1 and messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert BACKGROUND_START not in content and "INITIAL_CAMPAIGN_ONLY" not in content
    assert "CHILD_ASSIGNMENT" in content and "create_vulnerability_report" in content
    assert ("create_internal_finding" in content) is (scan_type == "internal")
    assert "https://scope.invalid" in content


def test_clean_internal_child_keeps_operator_constraints_and_access_details():
    spec = ScanSpec(
        target="10.70.0.0/24", scan_type="internal", socks5_proxy="127.0.0.1:19080",
        instruction_text="Do not change account membership.",
    )
    services = EngineServices(spec=spec)
    content = _child_initial_input(services, "reviewer", "child", EngineContext(), "Review", [])[0]["content"]
    assert "10.70.0.0/24" in content
    assert "127.0.0.1:19080" in content and "socks5_network" in content
    assert spec.instruction_text in content
    assert "Initial verified target host: none" in content
    assert "Initial verified remote session: none" in content
    assert BACKGROUND_START not in content


async def test_missing_explicit_skill_returns_spawn_failure_without_creating_task(tmp_path):
    events = EventWriter(tmp_path)
    coordinator = AgentCoordinator(tmp_path, events)
    services = EngineServices(coordinator=coordinator, events=events, spec=ScanSpec("https://scope.invalid"))
    spawn = make_spawn_child(services)
    result = spawn(
        name="reviewer", task="Review", parent_history=[], skills=["internal/missing"], parent=EngineContext(),
    )
    assert result["ok"] is False and "internal/missing" in result["error"]
    assert not coordinator._tasks


class ScriptedModel(Model):
    """In-memory responses; no provider credential or target connection."""

    model = "context-inheritance-fake"

    def __init__(self, steps):
        self.steps = deque(steps)
        self.inputs: list[list[Any]] = []

    async def get_response(self, *args, **kwargs):
        items = kwargs["input"] if "input" in kwargs else args[1]
        self.inputs.append(copy.deepcopy(items))
        step = self.steps.popleft()
        index = len(self.inputs)
        if isinstance(step, dict):
            output = ResponseFunctionToolCall(
                id=f"fc_{index}",
                call_id=f"call_{index}",
                name=step["tool"],
                arguments=json.dumps(step.get("arguments", {})),
                type="function_call",
                status="completed",
            )
        else:
            output = ResponseOutputMessage(
                id=f"msg_{index}",
                role="assistant",
                status="completed",
                type="message",
                content=[ResponseOutputText(type="output_text", text=step, annotations=[])],
            )
        return ModelResponse(
            output=[output], response_id=f"response_{index}", usage=Usage(requests=1, total_tokens=2)
        )

    async def stream_response(self, *args, **kwargs):
        yield completed_stream_event(await self.get_response(*args, **kwargs), self.model)


@pytest.mark.parametrize("inherit_context", [True, False])
async def test_real_sdk_spawn_inherits_cycle_input_and_keeps_sessions_isolated(tmp_path, inherit_context):
    events = EventWriter(tmp_path)
    coordinator = AgentCoordinator(tmp_path, events)
    await coordinator.register("root", "root", parent_id=None, task="Authorized parent assignment")
    sessions = {}

    def session_for(agent_id):
        if agent_id not in sessions:
            sessions[agent_id] = open_agent_session(agent_id, tmp_path / ".state" / "agents.db")
        return sessions[agent_id]

    history = [
        {"role": "user", "content": "Authorized target https://scope.invalid"},
        {"role": "assistant", "content": "PRIOR_CYCLE_FINDING: login at /login, cookie login_session"},
    ]
    root_session = session_for("root")
    await root_session.add_items(history)
    child_model = ScriptedModel([{
        "tool": "agent_finish",
        "arguments": {"result_summary": "CHILD_ONLY_RESULT", "report_to_parent": False},
    }])
    services = EngineServices(
        coordinator=coordinator,
        events=events,
        spec=ScanSpec("https://scope.invalid"),
        root_input="INITIAL_CAMPAIGN_ONLY",
        model_for=lambda name: child_model,
        session_for=session_for,
        context_settings=ContextSettings(STRIX_CONTEXT_AUTO_COMPACT=False),
    )
    services.spawn_child = make_spawn_child(services)
    parent = EngineContext(agent_id="root", agent_name="root", services=services, run_state=Mock())

    @function_tool
    def local_evidence() -> str:
        """Observe a deterministic fact within the current SDK execution cycle."""
        return "SAME_CYCLE_TOOL_RESULT"

    @function_tool
    async def wait_for_local_child() -> str:
        """Wait for the local deterministic child fixture to finish."""
        await asyncio.wait_for(asyncio.gather(*coordinator._tasks.values()), timeout=5)
        return "Child completed"

    parent_model = ScriptedModel(
        [
            {"tool": "local_evidence"},
            {
                "tool": "create_agent",
                "arguments": {
                    "name": "validator",
                    "task": "CHILD_ASSIGNMENT: validate only /login at https://scope.invalid",
                    "inherit_context": inherit_context,
                    "skills": ["tooling/python"],
                },
            },
            {"tool": "wait_for_local_child"},
            {
                "tool": "finish_scan",
                "arguments": {
                    "executive_summary": "Fixture complete",
                    "methodology": "Local fixture",
                    "technical_analysis": "No external systems accessed",
                    "recommendations": "None",
                },
            },
        ]
    )
    try:
        payload = await run_agent_loop(
            Agent(
                name="root",
                model=parent_model,
                tools=[local_evidence, create_agent, wait_for_local_child, finish_scan],
                tool_use_behavior=StopAtTools(stop_at_tool_names=["finish_scan"]),
            ),
            parent,
            initial_input="INITIAL_CAMPAIGN_ONLY",
            events=events,
            coordinator=coordinator,
            session=root_session,
        )
        await asyncio.wait_for(asyncio.gather(*coordinator._tasks.values()), timeout=5)
        assert payload["scan_completed"] is True and payload["success"] is True
        child_id = coordinator.children_of("root")[0]
        assert len(child_model.inputs) == 1
        child_input = child_model.inputs[0]
        assert len(child_input) == 1 and child_input[0]["role"] == "user"
        content = child_input[0]["content"]
        if inherit_context:
            assert inherited_history(content) == history
        else:
            assert BACKGROUND_START not in content and "PRIOR_CYCLE_FINDING" not in content
        # SDK 0.19 and the original Strix inherit the cycle's original input,
        # not tool outputs generated later in that same cycle.
        assert "SAME_CYCLE_TOOL_RESULT" in json.dumps(parent_model.inputs[1])
        assert "SAME_CYCLE_TOOL_RESULT" not in content
        assert "INITIAL_CAMPAIGN_ONLY" not in content
        assert "create_agent" not in content
        assert f"validator ({child_id}); your parent is root" in content
        parent_items = await root_session.get_items()
        child_items = await sessions[child_id].get_items()
        assert parent_items[: len(history)] == history
        assert child_items[0] == child_input[0]
        assert "CHILD_ONLY_RESULT" in json.dumps(child_items)
        assert "CHILD_ONLY_RESULT" not in json.dumps(parent_items)
        assert sessions[child_id] is not root_session
        assert coordinator.entry_of(child_id)["status"] == "completed"
        emitted = [json.loads(line) for line in events.path.read_text().splitlines()]
        creation = next(
            event
            for event in emitted
            if event["event_type"] == "agent.created" and event["actor"]["agent_id"] == child_id
        )
        assert creation["payload"]["parent_id"] == "root"
        assert creation["payload"]["skills"] == ["tooling/python"]
    finally:
        for task in coordinator._tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*coordinator._tasks.values(), return_exceptions=True)
        for session in sessions.values():
            session.close()
