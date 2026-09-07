"""Exercise configured SDK shell tools against an in-memory session only."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents import FunctionTool
from agents.sandbox.capabilities import Shell
from agents.sandbox.runtime_agent_preparation import clone_capabilities
from agents.tool_context import ToolContext

from strixops.agents.factory import (
    _bounded_tools,
    _configure_capability_tools,
    _sandbox_capabilities,
    _with_bounded_result,
    build_child_agent,
    root_tools,
)
from strixops.runtime.filesystem import SandboxFilesystem
from strixops.tools.output_store import configure_spill_writer


@pytest.fixture(autouse=True)
def clean_output_settings(monkeypatch):
    for suffix in ("TOKENS", "LINES", "BYTES"):
        monkeypatch.delenv(f"STRIX_TOOL_OUTPUT_MAX_{suffix}", raising=False)
    configure_spill_writer(None)
    yield
    configure_spill_writer(None)


class MemorySession:
    def __init__(self, output="ok", *, pty=True):
        self.output = output
        self.pty = pty
        self.calls = []

    def supports_pty(self):
        return self.pty

    def update(self):
        return SimpleNamespace(
            output=self.output.encode(), exit_code=0, process_id=None, original_token_count=None
        )

    async def pty_exec_start(self, command, **kwargs):
        self.calls.append(("exec_command", command, kwargs))
        return self.update()

    async def pty_write_stdin(self, **kwargs):
        self.calls.append(("write_stdin", None, kwargs))
        return self.update()

    async def exec(self, command, **kwargs):
        self.calls.append(("exec", command, kwargs))
        return SimpleNamespace(stdout=self.output.encode(), stderr=b"", exit_code=0)


def configured_tools(session):
    shell = Shell(configure_tools=_configure_capability_tools)
    shell.bind(session)
    return {tool.name: tool for tool in shell.tools()}


async def invoke(tool, args):
    raw = args if isinstance(args, str) else json.dumps(args)
    context = ToolContext(
        context=None, tool_name=tool.name, tool_call_id="output-cap-regression", tool_arguments=raw
    )
    return await tool.on_invoke_tool(context, raw)


@pytest.mark.parametrize("name", ["exec_command", "write_stdin"])
@pytest.mark.parametrize("requested,expected", [(None, 8000), (9000, 8000), (125, 125), ("125", 8000)])
async def test_real_sdk_tools_receive_the_ceiling_or_smaller_limit(name, requested, expected):
    session = MemorySession()
    args = {"cmd": "fixture"} if name == "exec_command" else {"session_id": 7, "chars": ""}
    args["max_output_tokens"] = requested
    output = await invoke(configured_tools(session)[name], args)
    assert session.calls[0][2]["max_output_tokens"] == expected
    assert output.endswith("Output:\nok")


@pytest.mark.parametrize("name", ["exec_command", "write_stdin"])
async def test_omitted_limit_uses_environment_ceiling(name, monkeypatch):
    monkeypatch.setenv("STRIX_TOOL_OUTPUT_MAX_TOKENS", "500")
    session = MemorySession()
    args = {"cmd": "fixture"} if name == "exec_command" else {"session_id": 7}
    await invoke(configured_tools(session)[name], args)
    assert session.calls[0][2]["max_output_tokens"] == 500


@pytest.mark.parametrize("name", ["exec_command", "write_stdin"])
async def test_large_shell_result_is_spilled_and_bounded(name, monkeypatch):
    monkeypatch.setenv("STRIX_TOOL_OUTPUT_MAX_LINES", "12")
    monkeypatch.setenv("STRIX_TOOL_OUTPUT_MAX_BYTES", "1024")
    session = MemorySession("\n".join(f"row {index:04d}" for index in range(100)))
    stored = []

    async def writer(identity, text):
        stored.append(text)
        return f"/workspace/.tool-output/{identity}.txt"

    configure_spill_writer(writer)
    args = {"cmd": "fixture"} if name == "exec_command" else {"session_id": 7}
    result = await invoke(configured_tools(session)[name], args)
    assert len(stored) == 1
    assert stored[0].endswith(session.output)
    assert "Process exited with code 0" in result
    assert "row 0099" in result
    assert "row 0050" not in result
    assert "full output saved to /workspace/.tool-output/" in result
    assert len(result.encode()) <= 1024


async def test_non_pty_sdk_execution_actually_truncates_tokens(monkeypatch):
    monkeypatch.setenv("STRIX_TOOL_OUTPUT_MAX_TOKENS", "40")
    session = MemorySession("sample output " * 1000, pty=False)
    tools = configured_tools(session)
    assert set(tools) == {"exec_command"}
    result = await invoke(tools["exec_command"], {"cmd": "fixture"})
    assert "Original token count:" in result
    assert len(result) < len(session.output)
    assert "truncated" in result.lower()


@pytest.mark.parametrize("raw", ["not json", "[]", '{"chars":"x"}', '{"session_id":7,"max_output_tokens":0}'])
async def test_argument_errors_still_return_results_without_running(raw):
    session = MemorySession()
    result = await invoke(configured_tools(session)["write_stdin"], raw)
    assert "error" in json.loads(result)
    assert session.calls == []


async def test_cancellation_is_not_converted_to_tool_output():
    session = MemorySession()

    async def cancelled(**_kwargs):
        raise asyncio.CancelledError

    session.pty_write_stdin = cancelled
    with pytest.raises(asyncio.CancelledError):
        await invoke(configured_tools(session)["write_stdin"], {"session_id": 7})


async def test_function_bounding_is_idempotent_and_preserves_non_string_results(monkeypatch):
    returned = {"success": True}
    calls = []

    async def inner(_ctx, _raw):
        calls.append(True)
        return returned

    tool = FunctionTool(name="fixture", description="Fixture", params_json_schema={}, on_invoke_tool=inner)
    _with_bounded_result(tool)
    wrapped = tool.on_invoke_tool
    _with_bounded_result(tool)
    assert tool.on_invoke_tool is wrapped
    assert await invoke(tool, {}) is returned
    assert calls == [True]
    other = object()
    assert _bounded_tools([other]) == [other]


async def test_small_lifecycle_json_stays_exact():
    payload = '{"success": true, "scan_completed": true}'

    async def inner(_ctx, _raw):
        return payload

    tool = FunctionTool(
        name="finish_scan", description="Fixture", params_json_schema={}, on_invoke_tool=inner
    )
    assert await invoke(_with_bounded_result(tool), {}) == payload


def test_root_and_child_function_tools_and_filesystem_results_are_bounded():
    root = root_tools()
    child = build_child_agent("child", "fixture", model="scripted")
    assert all(getattr(tool, "_strix_bounded", False) for tool in [*root, *child.tools])
    shared = next(tool for tool in root if tool.name == "think")
    assert next(tool for tool in child.tools if tool.name == "think") is shared
    capabilities = clone_capabilities(_sandbox_capabilities())
    assert [capability.type for capability in capabilities] == ["shell", "filesystem"]
    capabilities[1].bind(MemorySession())
    file_tools = capabilities[1].tools()
    assert [tool.name for tool in file_tools] == ["apply_patch", "view_image"]
    assert all(getattr(tool, "_strix_bounded", False) for tool in file_tools)


async def test_large_patch_error_is_bounded_and_spilled_before_entering_history():
    session = MemorySession()
    capability = _sandbox_capabilities()[1]
    capability.bind(session)
    tool = capability.tools()[0]
    stored = []

    async def writer(identity, text):
        stored.append(text)
        return f"/workspace/.tool-output/{identity}.txt"

    configure_spill_writer(writer)
    patch = "*** Begin Patch\n*** Add File: fixture.txt\n" + "X" * 60000 + "\n*** End Patch"
    result = await invoke(tool, {"command": patch})
    assert len(stored) == 1
    complete = json.loads(stored[0])
    assert complete["success"] is False
    assert complete["error"].startswith("ValueError: Invalid Add File line:")
    assert len(stored[0].encode()) > 50 * 1024
    assert len(result.encode()) <= 50 * 1024
    assert "full output saved to /workspace/.tool-output/" in result
    assert session.calls == []


async def test_patch_output_hook_keeps_small_error_json_and_command_schema():
    plain = SandboxFilesystem()
    plain.bind(MemorySession())
    original = plain.tools()[0]
    configured = _sandbox_capabilities()[1]
    configured.bind(MemorySession())
    bounded = configured.tools()[0]
    assert bounded.params_json_schema == original.params_json_schema
    assert bounded.params_json_schema["required"] == ["command"]
    assert bounded.params_json_schema["properties"]["command"]["type"] == "string"
    assert getattr(bounded, "_strix_bounded", False)
    args = {"command": "invalid patch"}
    result = await invoke(bounded, args)
    assert result == await invoke(original, args)
    assert json.loads(result)["success"] is False
