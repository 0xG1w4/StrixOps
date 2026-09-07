"""JSON patch tools use the SDK editor against a bound in-memory sandbox.

The streamed regression uses a mocked HTTP transport for Chat Completions;
no container, target connection, API credential or host file write is needed.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agents import Agent, FunctionTool, RunConfig, RunContextWrapper, Runner
from agents.models.chatcmpl_converter import Converter
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.sandbox import Manifest
from agents.sandbox.runtime_agent_preparation import clone_capabilities, prepare_sandbox_agent
from agents.sandbox.workspace_paths import WorkspacePathPolicy
from agents.tool_context import ToolContext
from openai import AsyncOpenAI

from strixops.agents.factory import build_child_agent, build_root_agent
from strixops.engine.scanconfig import ScanSpec
from strixops.runtime.filesystem import SandboxFilesystem
from strixops.testing.scripted_gateway import NonStreamingModel, _completion


class MemorySandbox:
    """Only the session file API is available: patches cannot run shell code."""

    def __init__(self):
        self.state = SimpleNamespace(manifest=Manifest(root="/workspace"))
        self.files: dict[Path, bytes] = {}
        self.calls: list[tuple] = []
        self.failure: BaseException | None = None
        self.policy = WorkspacePathPolicy(root="/workspace")

    def supports_pty(self):
        return True

    def _workspace_path_policy(self):
        return self.policy

    def normalize_path(self, path, **kwargs):
        return self.policy.normalize_path(path, **kwargs)

    def _before_io(self, action, path, user):
        self.calls.append((action, Path(path), user))
        if self.failure:
            raise self.failure

    async def read(self, path, *, user=None):
        self._before_io("read", path, user)
        if Path(path) not in self.files:
            raise FileNotFoundError(str(path))
        return io.BytesIO(self.files[Path(path)])

    async def mkdir(self, path, *, parents=False, user=None):
        self._before_io("mkdir", path, user)

    async def write(self, path, data, *, user=None):
        self._before_io("write", path, user)
        self.files[Path(path)] = data.read()

    async def rm(self, path, *, user=None):
        self._before_io("rm", path, user)
        del self.files[Path(path)]


def patch_tool(session, *, user=None):
    capability = SandboxFilesystem()
    capability.bind(session)
    capability.bind_run_as(user)
    return capability.tools()[0]


async def invoke(tool, arguments):
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    context = ToolContext(
        context=None, tool_name=tool.name, tool_call_id="patch-regression", tool_arguments=raw
    )
    return json.loads(await tool.on_invoke_tool(context, raw))


def prepare(kind, session):
    spec = ScanSpec(target="https://scan.invalid")
    if kind == "root":
        agent = build_root_agent(spec, model="scripted", sandbox=True)
    else:
        agent = build_child_agent("child", "Write a helper", model="scripted", sandbox=True, spec=spec)
    capabilities = clone_capabilities(agent.capabilities)
    for capability in capabilities:
        capability.bind(session)
    return prepare_sandbox_agent(agent=agent, session=session, capabilities=capabilities)


@pytest.mark.parametrize("kind", ["root", "child"])
def test_live_agents_register_one_function_patch_alongside_shell(kind):
    agent = prepare(kind, MemorySandbox())
    tools = [tool for tool in agent.tools if tool.name == "apply_patch"]
    assert len(tools) == 1
    assert isinstance(tools[0], FunctionTool)
    names = {tool.name for tool in agent.tools}
    assert {"exec_command", "write_stdin", "apply_patch"} <= names
    assert "view_image" in names
    converted = [Converter.tool_to_openai(tool) for tool in agent.tools]
    patch = next(tool for tool in converted if tool["function"]["name"] == "apply_patch")
    assert patch["type"] == "function"
    assert patch["function"]["parameters"]["required"] == ["command"]
    assert patch["function"]["parameters"]["properties"]["command"]["type"] == "string"


@pytest.mark.asyncio
async def test_root_and_child_prompts_describe_json_patch_arguments():
    for kind in ("root", "child"):
        agent = prepare(kind, MemorySandbox())
        instructions = await agent.instructions(RunContextWrapper(context=None), agent)
        assert "apply_patch" in instructions
        assert "JSON command field" in instructions


def test_unbound_capability_cannot_construct_a_patch_tool():
    with pytest.raises(ValueError, match="not bound"):
        SandboxFilesystem().tools()


@pytest.mark.asyncio
async def test_create_update_and_delete_delegate_to_sandbox_editor():
    sandbox = MemorySandbox()
    tool = patch_tool(sandbox, user="sandbox-user")
    created = await invoke(
        tool, {"command": "*** Begin Patch\n*** Add File: recon/fetch.py\n+first\n*** End Patch"}
    )
    assert created == {"success": True, "output": "Created recon/fetch.py"}
    assert sandbox.files[Path("/workspace/recon/fetch.py")] == b"first"
    updated = await invoke(
        tool,
        {"command": "*** Begin Patch\n*** Update File: recon/fetch.py\n@@\n-first\n+second\n*** End Patch"},
    )
    assert updated == {"success": True, "output": "Updated recon/fetch.py"}
    assert sandbox.files[Path("/workspace/recon/fetch.py")] == b"second"
    deleted = await invoke(
        tool, {"command": "*** Begin Patch\n*** Delete File: recon/fetch.py\n*** End Patch"}
    )
    assert deleted == {"success": True, "output": "Deleted recon/fetch.py"}
    assert not sandbox.files
    assert all(user == "sandbox-user" for _, _, user in sandbox.calls)


@pytest.mark.asyncio
async def test_invalid_patch_or_arguments_return_errors_without_writes():
    sandbox = MemorySandbox()
    tool = patch_tool(sandbox)
    for arguments in ({}, {"command": 7}, "not json", {"command": "not a patch"}):
        result = await invoke(tool, arguments)
        assert result["success"] is False
        assert result["error"] and result["hint"]
    assert not sandbox.calls and not sandbox.files


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside.txt", "/tmp/outside.txt"])
async def test_patch_uses_sdk_workspace_boundary(path):
    sandbox = MemorySandbox()
    result = await invoke(
        patch_tool(sandbox), {"command": f"*** Begin Patch\n*** Add File: {path}\n+outside\n*** End Patch"}
    )
    assert not result["success"]
    assert not sandbox.calls


@pytest.mark.asyncio
async def test_missing_file_bad_diff_and_io_failures_are_recoverable():
    sandbox = MemorySandbox()
    tool = patch_tool(sandbox)
    missing = await invoke(tool, {"command": "*** Begin Patch\n*** Delete File: missing.txt\n*** End Patch"})
    assert not missing["success"] and "missing.txt" in missing["error"]
    sandbox.files[Path("/workspace/existing.txt")] = b"keep me\n"
    bad_diff = await invoke(
        tool,
        {
            "command": (
                "*** Begin Patch\n*** Update File: existing.txt\n@@\n-not present\n+changed\n*** End Patch"
            )
        },
    )
    assert not bad_diff["success"]
    assert sandbox.files[Path("/workspace/existing.txt")] == b"keep me\n"
    sandbox.failure = PermissionError("sandbox write denied")
    denied = await invoke(tool, {"command": "*** Begin Patch\n*** Add File: new.txt\n+new\n*** End Patch"})
    assert not denied["success"] and "sandbox write denied" in denied["error"]
    assert Path("/workspace/new.txt") not in sandbox.files


@pytest.mark.asyncio
async def test_cancellation_propagates_instead_of_becoming_a_tool_error():
    sandbox = MemorySandbox()
    sandbox.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await invoke(
            patch_tool(sandbox), {"command": "*** Begin Patch\n*** Add File: new.txt\n+new\n*** End Patch"}
        )


@pytest.mark.asyncio
async def test_same_patch_paths_in_separate_bound_sessions_do_not_share_files():
    first, second = MemorySandbox(), MemorySandbox()
    template = SandboxFilesystem()
    left, right = template.clone(), template.clone()
    left.bind(first)
    right.bind(second)
    await invoke(
        left.tools()[0], {"command": "*** Begin Patch\n*** Add File: same.txt\n+left\n*** End Patch"}
    )
    await invoke(
        right.tools()[0], {"command": "*** Begin Patch\n*** Add File: same.txt\n+right\n*** End Patch"}
    )
    assert first.files[Path("/workspace/same.txt")] == b"left"
    assert second.files[Path("/workspace/same.txt")] == b"right"


@pytest.mark.asyncio
async def test_chatcompletions_stream_executes_patch_and_continues_without_model_behavior_error():
    # Match the failed live run's function arguments: command contains a full
    # patch adding recon/fetch.py. The generated file is never executed.
    patch = (
        "*** Begin Patch\n*** Add File: recon/fetch.py\n"
        "+#!/usr/bin/env python3\n+print('helper')\n*** End Patch"
    )
    responses = [
        _completion({"tool_calls": [{"name": "apply_patch", "arguments": {"command": patch}}]}, 0),
        _completion({"text": "File created; continuing the task."}, 1),
    ]
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    session = MemorySandbox()
    prepared = prepare("root", session)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = AsyncOpenAI(
            base_url="https://scripted.invalid/v1", api_key="test-placeholder", http_client=http_client
        )
        model = NonStreamingModel(OpenAIChatCompletionsModel(model="scripted", openai_client=client))
        # Run the actually prepared tool list through real SDK streamed tool
        # planning. HTTP is fully in-memory and no SandboxRuntime is started.
        agent = Agent(name="patch regression", tools=prepared.tools, model=model)
        result = Runner.run_streamed(
            agent, input="Create the helper.", max_turns=3, run_config=RunConfig(tracing_disabled=True)
        )
        async for _ in result.stream_events():
            pass
    assert result.final_output == "File created; continuing the task."
    assert session.files[Path("/workspace/recon/fetch.py")] == b"#!/usr/bin/env python3\nprint('helper')"
    assert len(calls) == 2
    assert any(tool["function"]["name"] == "apply_patch" for tool in calls[0]["tools"])
    outputs = [message for message in calls[1]["messages"] if message["role"] == "tool"]
    assert any(json.loads(message["content"])["success"] for message in outputs)
