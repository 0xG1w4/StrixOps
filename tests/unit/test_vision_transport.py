"""Exercise sandbox image results through real SDK calls and mock HTTP.

No container, screenshot, external model request, or host image is needed.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from pathlib import Path

import httpx
import pytest
from agents import Agent, RunConfig, Runner, SQLiteSession, ToolOutputImage
from agents.models.chatcmpl_converter import Converter
from agents.tool_context import ToolContext
from openai import AsyncOpenAI

from strixops.config import provider
from strixops.config.settings import EngineSettings
from strixops.config.vision_transport import PlatformChatCompletionsModel, project_tool_images
from strixops.runtime.filesystem import SandboxFilesystem
from strixops.testing.scripted_gateway import _completion
from tests.unit.test_filesystem_tools import MemorySandbox, prepare

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9X8AAAAASUVORK5CYII="
)
IMAGE_URL = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")


def image_tool(sandbox, *, user=None):
    capability = SandboxFilesystem()
    capability.bind(sandbox)
    capability.bind_run_as(user)
    return next(tool for tool in capability.tools() if tool.name == "view_image")


async def invoke_image(tool, arguments):
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    context = ToolContext(context=None, tool_name="view_image", tool_call_id="image", tool_arguments=raw)
    return await tool.on_invoke_tool(context, raw)


@pytest.mark.asyncio
async def test_image_reader_returns_structured_sandbox_content_with_run_user():
    sandbox = MemorySandbox()
    sandbox.files[Path("/workspace/screenshot.png")] = PNG
    result = await invoke_image(image_tool(sandbox, user="sandbox-user"), {"path": "screenshot.png"})
    assert isinstance(result, ToolOutputImage)
    assert result.image_url == IMAGE_URL
    assert sandbox.calls == [("read", Path("/workspace/screenshot.png"), "sandbox-user")]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"path": ""}, {"path": 4}, "invalid json"])
async def test_invalid_image_arguments_are_recoverable_tool_results(arguments):
    sandbox = MemorySandbox()
    result = json.loads(await invoke_image(image_tool(sandbox), arguments))
    assert result["error"] and result["hint"]
    assert not sandbox.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside.png", "/tmp/outside.png"])
async def test_image_reader_uses_workspace_boundary(path):
    sandbox = MemorySandbox()
    result = json.loads(await invoke_image(image_tool(sandbox), {"path": path}))
    assert result["error"]
    assert not sandbox.calls


@pytest.mark.asyncio
async def test_missing_unsupported_and_oversized_images_return_sdk_errors():
    sandbox = MemorySandbox()
    tool = image_tool(sandbox)
    assert "not found" in await invoke_image(tool, {"path": "missing.png"})
    sandbox.files[Path("/workspace/plain.txt")] = b"plain text"
    assert "not a supported image" in await invoke_image(tool, {"path": "plain.txt"})
    sandbox.files[Path("/workspace/large.png")] = b"x" * (10 * 1024 * 1024 + 1)
    assert "10MB" in await invoke_image(tool, {"path": "large.png"})


@pytest.mark.asyncio
async def test_image_read_cancellation_propagates():
    sandbox = MemorySandbox()
    sandbox.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await invoke_image(image_tool(sandbox), {"path": "screenshot.png"})


def _call(call_id, name="view_image"):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def _image_output(call_id, *, text=None):
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": [
            *([{"type": "input_text", "text": text}] if text else []),
            {"type": "input_image", "image_url": IMAGE_URL, "detail": "high"},
        ],
    }


def test_projection_keeps_parallel_call_pairing_and_does_not_mutate_session_history():
    items = [
        _call("image"),
        _call("text", "exec_command"),
        _image_output("image", text="Saved screenshot"),
        {"type": "function_call_output", "call_id": "text", "output": "Command finished"},
        {"role": "user", "content": "Continue checking the page"},
    ]
    original = copy.deepcopy(items)
    projected = project_tool_images(items)
    assert items == original
    converted = Converter.items_to_messages(projected)
    assert [item["role"] for item in converted] == ["assistant", "tool", "tool", "user", "user"]
    assert converted[1]["tool_call_id"] == "image"
    assert converted[1]["content"] == [{"type": "text", "text": "Saved screenshot"}]
    assert converted[2]["tool_call_id"] == "text"
    image = converted[3]["content"][1]
    # Match the original LiteLLM converter's image data and detail, relocating
    # only its role because direct Chat Completions tool messages are textual.
    reference = Converter.items_to_messages(items[:3], preserve_tool_output_all_content=True)
    assert image == reference[-1]["content"][1]


def test_text_only_input_and_tool_results_remain_unchanged():
    assert project_tool_images("plain prompt") == "plain prompt"
    items = [
        _call("text", "exec_command"),
        {"type": "function_call_output", "call_id": "text", "output": "ok"},
    ]
    assert project_tool_images(items) == items


def _stream(completion):
    choice = completion["choices"][0]
    message = copy.deepcopy(choice["message"])
    for index, call in enumerate(message.get("tool_calls", [])):
        call["index"] = index
    common = {
        "id": completion["id"],
        "object": "chat.completion.chunk",
        "created": 0,
        "model": completion["model"],
    }
    chunks = [
        {**common, "choices": [{"index": 0, "delta": message, "finish_reason": None}]},
        {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]},
        {**common, "choices": [], "usage": completion["usage"]},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    ("base_url", "model_name", "wire_model"),
    [
        ("https://compatible.invalid/v1", "my-vision-model", "my-vision-model"),
        ("https://openrouter.ai/api/v1", "openrouter/openai/vision-model", "openai/vision-model"),
    ],
)
async def test_real_sdk_image_tool_reaches_existing_route_on_both_response_paths(
    monkeypatch, streaming, base_url, model_name, wire_model
):
    completions = [
        _completion({"tool_calls": [{"name": "view_image", "arguments": {"path": "screenshot.png"}}]}, 0),
        _completion({"text": "The screenshot is visible; continuing."}, 1),
    ]
    requests = []
    bodies = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        bodies.append(body)
        completion = completions.pop(0)
        return _stream(completion) if body.get("stream") else httpx.Response(200, json=completion)

    sandbox = MemorySandbox()
    sandbox.files[Path("/workspace/screenshot.png")] = PNG
    prepared = prepare("root", sandbox)
    settings = EngineSettings(base_url, "test-placeholder", model_name, "", "", "", False)
    session = SQLiteSession("vision-transport")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        monkeypatch.setattr(
            provider, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(**kwargs, http_client=http_client)
        )
        model = provider.make_platform_model(settings)
        assert isinstance(model, PlatformChatCompletionsModel)
        agent = Agent(name="vision regression", model=model, tools=prepared.tools)
        kwargs = {
            "input": "Inspect the saved screenshot.",
            "session": session,
            "max_turns": 3,
            "run_config": RunConfig(tracing_disabled=True),
        }
        if streaming:
            result = Runner.run_streamed(agent, **kwargs)
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(agent, **kwargs)
        history = await session.get_items()
    session.close()

    assert result.final_output == "The screenshot is visible; continuing."
    assert len(requests) == 2
    assert all(str(request.url) == base_url + "/chat/completions" for request in requests)
    assert all(request.headers["authorization"] == "Bearer test-placeholder" for request in requests)
    assert all(body["model"] == wire_model for body in bodies)
    assert all(bool(body.get("stream")) is streaming for body in bodies)
    messages = bodies[1]["messages"]
    assistant = next(message for message in messages if message.get("tool_calls"))
    tool = next(message for message in messages if message["role"] == "tool")
    assert tool["tool_call_id"] == assistant["tool_calls"][0]["id"]
    assert isinstance(tool["content"], str) and tool["content"]
    images = [
        part
        for message in messages
        if message["role"] == "user" and isinstance(message["content"], list)
        for part in message["content"]
        if part["type"] == "image_url"
    ]
    assert len(images) == 1
    assert images[0]["image_url"]["url"] == IMAGE_URL
    outputs = [item for item in history if item.get("type") == "function_call_output"]
    assert outputs[0]["output"][0]["type"] == "input_image"
    assert outputs[0]["output"][0]["image_url"] == IMAGE_URL
