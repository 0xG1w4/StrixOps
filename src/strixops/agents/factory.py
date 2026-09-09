"""Agent factory: root and child agents over the shared tool table.

Live runs build :class:`SandboxAgent` instances carrying the SDK's Shell
and our JSON patch capability (exec_command / write_stdin / apply_patch,
bound to the run's sandbox session via ``RunConfig.sandbox``). Dry
runs (no sandbox) build plain agents — the capability tools need a live
container.

The lifecycle termination rule is final: the run stops only when
``finish_scan`` (web/root) or ``agent_finish`` (child) succeeds.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from agents import Agent, FunctionTool, StopAtTools

from strixops.agents.prompts import child_instructions, root_instructions
from strixops.engine.scanconfig import ScanSpec
from strixops.tools.assessment import (
    amend_threat_model,
    get_threat_model,
    list_coverage,
    record_coverage,
    save_threat_model,
    update_coverage,
)
from strixops.tools.collaboration import (
    create_agent,
    send_message_to_agent,
    stop_agent,
    view_agent_graph,
    wait_for_agents,
)
from strixops.tools.internal_campaign import get_internal_campaign, record_internal_event
from strixops.tools.lifecycle import agent_finish, finish_scan
from strixops.tools.output_store import bound_and_store
from strixops.tools.proxy import proxy_tools
from strixops.tools.reporting import (
    create_dependency_report,
    create_finding,
    create_vulnerability_report,
    get_report,
    list_reports,
    update_vulnerability_report,
)
from strixops.tools.skills import list_skills, load_skill
from strixops.tools.web_search import web_search
from strixops.tools.workspace_tools import create_todo, think, update_todo

ROOT_AGENT_NAME = "root agent"

LIFECYCLE_TOOLS = {"finish_scan", "agent_finish"}


def _resource_tools(tools: list[Any], resources: Any) -> list[Any]:
    if resources is None:
        return tools
    result: list[Any] = []
    for tool in tools:
        if not isinstance(tool, FunctionTool):
            result.append(tool)
            continue
        invoke = tool.on_invoke_tool

        async def with_resources(ctx: Any, raw_input: str, inner: Any = invoke) -> Any:
            with resources.activate():
                return await inner(ctx, raw_input)

        result.append(replace(tool, on_invoke_tool=with_resources))
    return result


def _resources(run_dir: Any, spec: ScanSpec | None) -> Any:
    if run_dir is None:
        return None
    from strixops.engine.prompt_resources import PromptResources

    return PromptResources.for_run(run_dir, spec)


def base_tools() -> list[Any]:
    return [
        think,
        create_todo,
        update_todo,
        load_skill,
        list_skills,
        record_coverage,
        update_coverage,
        list_coverage,
        save_threat_model,
        get_threat_model,
        amend_threat_model,
        list_reports,
        get_report,
        create_dependency_report,
        record_internal_event,
        get_internal_campaign,
        create_vulnerability_report,
        update_vulnerability_report,
        create_finding,
        web_search,
        *proxy_tools(),
    ]


def root_tools() -> list[Any]:
    return _bounded_tools(
        [
            *base_tools(),
            create_agent,
            wait_for_agents,
            send_message_to_agent,
            view_agent_graph,
            stop_agent,
            finish_scan,
        ]
    )


def child_tools() -> list[Any]:
    return _bounded_tools(
        [
            *base_tools(),
            create_agent,
            wait_for_agents,
            send_message_to_agent,
            view_agent_graph,
            stop_agent,
            agent_finish,
        ]
    )


def _tool_output_limits() -> tuple[int, int]:
    from strixops.config.context import ContextSettings

    context = ContextSettings()
    return context.tool_output_max_lines, context.tool_output_max_bytes


async def _bound_result(result: Any) -> Any:
    if not isinstance(result, str):
        return result
    max_lines, max_bytes = _tool_output_limits()
    return await bound_and_store(result, max_lines=max_lines, max_bytes=max_bytes)


def _with_bounded_result(tool: FunctionTool) -> FunctionTool:
    """Cap string results once, including when root and children share tools."""
    if getattr(tool, "_strix_bounded", False):
        return tool
    invoke_tool = tool.on_invoke_tool

    async def invoke(ctx: Any, raw_input: str) -> Any:
        return await _bound_result(await invoke_tool(ctx, raw_input))

    tool.on_invoke_tool = invoke
    tool._strix_bounded = True
    return tool


def _bounded_tools(tools: list[Any]) -> list[Any]:
    return [_with_bounded_result(tool) if isinstance(tool, FunctionTool) else tool for tool in tools]


def _apply_shell_output_cap(parsed: dict[str, Any]) -> None:
    """Use the configured ceiling unless the caller asks for fewer tokens."""
    from strixops.config.context import ContextSettings

    ceiling = ContextSettings().tool_output_max_tokens
    requested = parsed.get("max_output_tokens")
    parsed["max_output_tokens"] = (
        ceiling if not isinstance(requested, int) or requested > ceiling else requested
    )


def _with_shell_output_cap(tool: FunctionTool) -> FunctionTool:
    if getattr(tool, "_strix_shell_output_capped", False):
        return tool
    invoke_tool = tool.on_invoke_tool

    async def invoke(ctx: Any, raw_input: str) -> Any:
        try:
            parsed = json.loads(raw_input)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            _apply_shell_output_cap(parsed)
            raw_input = json.dumps(parsed)
        return await invoke_tool(ctx, raw_input)

    tool.on_invoke_tool = invoke
    tool._strix_shell_output_capped = True
    return tool


def _error_as_result(tool: Any) -> Any:
    """Patch a tool so invocation failures return to the model as results.

    Real models occasionally violate a tool schema (e.g. ``write_stdin``
    without ``session_id``); the SDK raises ``UserError`` which would
    otherwise kill the whole agent turn. Returning the error as the tool
    result lets the model correct itself on the next call — the same
    error-as-result discipline the platform's own tools use. Works on both
    capability tool dataclasses and FunctionTool (instance attribute shadows
    in both).
    """
    import json as _json

    inner = tool.on_invoke_tool

    async def wrapped(ctx: Any, input_json: str) -> Any:
        try:
            return await inner(ctx, input_json)
        except Exception as exc:  # noqa: BLE001 — schema/invocation errors are recoverable
            return _json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "hint": "fix the arguments (check required fields) and call the tool again",
                }
            )

    tool.on_invoke_tool = wrapped
    return tool


def _configure_capability_tools(toolset: Any) -> None:
    """``configure_tools`` hook: wrap every capability tool in the toolset."""
    for name in list(vars(toolset)):
        tool = getattr(toolset, name)
        if tool is None or not hasattr(tool, "on_invoke_tool"):
            continue
        if tool.name in {"exec_command", "write_stdin"}:
            _with_shell_output_cap(tool)
        _error_as_result(tool)
        if isinstance(tool, FunctionTool):
            _with_bounded_result(tool)


def _sandbox_capabilities() -> list[Any]:
    # Chat Completions accepts function tools, while the SDK's bundled
    # apply_patch is freeform. Our adapter keeps its sandbox editor behind
    # a normal JSON function and retains the existing Shell capability.
    from agents.sandbox.capabilities import Shell

    from strixops.runtime.filesystem import SandboxFilesystem

    return [
        Shell(configure_tools=_configure_capability_tools),
        SandboxFilesystem(configure_tools=_bounded_tools),
    ]


def build_root_agent(
    spec: ScanSpec,
    model: Any = None,
    *,
    sandbox: bool = False,
    run_dir: Any = None,
) -> Any:
    resources = _resources(run_dir, spec)
    with resources.activate() if resources is not None else nullcontext():
        instructions = root_instructions(resources.spec if resources is not None else spec)
    tools = _resource_tools(root_tools(), resources)
    if resources is not None:
        resources.record_prompt("root", ROOT_AGENT_NAME, instructions, tools)

    if sandbox:
        from agents.sandbox import SandboxAgent

        return SandboxAgent(
            name=ROOT_AGENT_NAME,
            instructions=instructions,
            tools=tools,
            tool_use_behavior=StopAtTools(stop_at_tool_names=["finish_scan"]),
            model=model,
            capabilities=_sandbox_capabilities(),
        )
    return Agent(
        name=ROOT_AGENT_NAME,
        instructions=instructions,
        tools=tools,
        model=model,
        tool_use_behavior=StopAtTools(stop_at_tool_names=["finish_scan"]),
    )


def build_child_agent(
    name: str,
    task: str,
    model: Any = None,
    *,
    sandbox: bool = False,
    spec: ScanSpec | None = None,
    skills: list[str] | None = None,
    run_dir: Any = None,
    agent_id: str | None = None,
) -> Any:
    resources = _resources(run_dir, spec)
    with resources.activate() if resources is not None else nullcontext():
        instructions = child_instructions(
            task,
            resources.spec if resources is not None else spec,
            skills=skills,
        )
    tools = _resource_tools(child_tools(), resources)
    if resources is not None:
        import uuid

        resources.record_prompt(agent_id or uuid.uuid4().hex[:8], name, instructions, tools)

    if sandbox:
        from agents.sandbox import SandboxAgent

        return SandboxAgent(
            name=name,
            instructions=instructions,
            tools=tools,
            tool_use_behavior=StopAtTools(stop_at_tool_names=["agent_finish"]),
            model=model,
            capabilities=_sandbox_capabilities(),
        )
    return Agent(
        name=name,
        instructions=instructions,
        tools=tools,
        model=model,
        tool_use_behavior=StopAtTools(stop_at_tool_names=["agent_finish"]),
    )
