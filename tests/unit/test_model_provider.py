"""Real SDK transport contracts against local mock HTTP, with no live models."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import httpx
import pytest
from agents import Agent, ModelSettings, ModelTracing, RunConfig, Runner, SQLiteSession, StopAtTools
from agents.models.default_models import get_default_model_settings
from openai.types.shared.reasoning import Reasoning

from strixops.config.model_options import resolved_api_mode, validate_model_options
from strixops.config.provider import make_platform_model
from strixops.config.responses_transport import PlatformResponsesModel
from strixops.config.settings import EngineSettings
from strixops.config.vision_transport import PlatformChatCompletionsModel
from strixops.engine.resilience import model_settings
from strixops.engine.scanconfig import EngineContext
from strixops.engine.usage import UsageAccumulator, UsageTrackingModel
from strixops.testing.scripted_gateway import _completion
from strixops.tools.lifecycle import agent_finish
from tests.unit.test_filesystem_tools import MemorySandbox, prepare
from tests.unit.test_vision_transport import IMAGE_URL, PNG, _stream


def _settings(model="gateway-alias", mode="chat_completions", effort="default"):
    return EngineSettings("https://gateway.invalid/v1", "fixture-key", model, "", "", "", False, mode, effort)


@pytest.mark.parametrize(
    ("model", "mode", "expected"),
    [
        ("gpt-6-astra", "auto", "responses"),
        ("openrouter/openai/gpt-6-astra", "auto", "responses"),
        ("litellm/openai/gpt-6-astra-2026-09-01", "auto", "responses"),
        ("gpt-5.4", "auto", "chat_completions"),
        ("gpt-5.4-pro", "auto", "responses"),
        ("gpt-5.5", "auto", "responses"),
        ("gpt-5.6", "auto", "responses"),
        ("openai/gpt-5.6-terra", "auto", "responses"),
        ("gpt-5.6-luna-2026-08-01", "auto", "responses"),
        ("custom-gpt-6-astra", "auto", "chat_completions"),
        ("gateway-alias", "responses", "responses"),
        ("gateway-alias", "chat_completions", "chat_completions"),
    ],
)
def test_auto_is_deterministic_and_preserves_explicit_choices(model, mode, expected):
    assert resolved_api_mode(model, mode) == expected


@pytest.mark.parametrize(
    ("model", "mode", "effort", "message"),
    [
        ("", "invalid", "default", "API mode"),
        ("", "auto", "ultra", "Reasoning effort"),
        ("gpt-6-astra", "chat_completions", "default", "requires Responses"),
        ("gpt-6-astra", "responses", "none", "supports reasoning effort"),
        ("openai/gpt-6-astra", "auto", "minimal", "supports reasoning effort"),
        ("gpt-5.4", "chat_completions", "high", "requires Responses"),
        ("gpt-5.5", "chat_completions", "medium", "requires Responses"),
        ("gpt-5.5", "chat_completions", "default", "defaults to medium"),
        ("gpt-5.6-sol", "chat_completions", "default", "defaults to medium"),
        ("gpt-5.4-pro", "chat_completions", "default", "requires Responses"),
        ("gpt-5.4-pro", "responses", "none", "supports reasoning effort"),
        ("gpt-5.4", "responses", "max", "supports reasoning effort"),
        ("gpt-5.5", "responses", "minimal", "supports reasoning effort"),
    ],
)
def test_known_invalid_routes_fail_before_http(model, mode, effort, message):
    assert any(message in error for error in validate_model_options(model, mode, effort))
    with pytest.raises(ValueError, match=message):
        make_platform_model(_settings(model, mode, effort))


@pytest.mark.parametrize(
    ("model", "mode", "effort"),
    [
        ("", "auto", "default"),
        ("gpt-6-astra", "auto", "default"),
        ("gpt-6-astra", "responses", "max"),
        ("gpt-5.4", "chat_completions", "none"),
        ("gpt-5.4", "responses", "high"),
        ("gpt-5.5", "chat_completions", "none"),
        ("gpt-5.6", "auto", "max"),
        ("gpt-5.4-pro", "auto", "default"),
        ("gateway-alias", "chat_completions", "max"),
    ],
)
def test_valid_and_unknown_model_options_are_not_guessed(model, mode, effort):
    assert validate_model_options(model, mode, effort) == []


def test_engine_env_options_and_legacy_defaults(monkeypatch):
    monkeypatch.delenv("LLM_API_MODE", raising=False)
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    settings = EngineSettings.from_env()
    assert settings.llm_api_mode == "chat_completions"
    assert settings.llm_reasoning_effort == "default"
    monkeypatch.setenv("LLM_API_MODE", " RESPONSES ")
    monkeypatch.setenv("LLM_REASONING_EFFORT", " HIGH ")
    configured = EngineSettings.from_env()
    assert configured.llm_api_mode == "responses"
    assert configured.llm_reasoning_effort == "high"
    assert _settings("gpt-6-astra").validate()
    assert not _settings("gpt-6-astra", "auto", "high").validate()


def _response(output, index=0):
    return {
        "id": f"resp_{index}",
        "object": "response",
        "created_at": 0,
        "model": "fixture-model",
        "status": "completed",
        "output": output,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 1},
            "total_tokens": 15,
        },
    }


def _tool_call(name, arguments, index):
    return {
        "type": "function_call",
        "id": f"fc_{index}",
        "call_id": f"call_{index}",
        "name": name,
        "arguments": json.dumps(arguments),
        "status": "completed",
    }


def _message(text):
    return {
        "type": "message",
        "id": "msg_fixture",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _response_stream(response):
    events = [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
        *[
            {"type": "response.output_item.done", "output_index": index, "item": item}
            for index, item in enumerate(response["output"])
        ],
        {"type": "response.completed", "response": response},
    ]
    body = "".join(
        f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': index})}\n\n"
        for index, event in enumerate(events)
    )
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("effort", ["default", "high"])
async def test_responses_image_tool_round_trip_lifecycle_and_usage(streaming, effort):
    from pathlib import Path

    output = [
        _response([_tool_call("view_image", {"path": "screenshot.png"}, 0)], 0),
        _response(
            [
                _tool_call(
                    "agent_finish", {"result_summary": "Screenshot checked", "report_to_parent": False}, 1
                )
            ],
            1,
        ),
    ]
    requests = []
    bodies = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        bodies.append(body)
        response = output.pop(0)
        return _response_stream(response) if body.get("stream") else httpx.Response(200, json=response)

    sandbox = MemorySandbox()
    sandbox.files[Path("/workspace/screenshot.png")] = PNG
    image = next(tool for tool in prepare("child", sandbox).tools if tool.name == "view_image")
    context = EngineContext(agent_id="child", agent_name="child", parent_id="root")
    session = SQLiteSession("responses-image")
    accumulator = UsageAccumulator()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            model = make_platform_model(
                _settings("openrouter/openai/gpt-6-astra", "auto", effort), http_client=client
            )
            assert isinstance(model, PlatformResponsesModel)
            agent = Agent(
                name="Responses fixture",
                model=UsageTrackingModel(model, accumulator),
                tools=[image, agent_finish],
                tool_use_behavior=StopAtTools(stop_at_tool_names=["agent_finish"]),
                # Simulate defaults already resolved by the SDK; route default must remove them.
                model_settings=ModelSettings(reasoning=Reasoning(effort="low")),
            )
            kwargs = {
                "input": "Inspect screenshot then finish.",
                "context": context,
                "session": session,
                "run_config": RunConfig(tracing_disabled=True, model_settings=model_settings()),
            }
            if streaming:
                result = Runner.run_streamed(agent, **kwargs)
                async for _ in result.stream_events():
                    pass
            else:
                result = await Runner.run(agent, **kwargs)
            history = await session.get_items()
    finally:
        session.close()

    assert json.loads(result.final_output)["agent_finished"] is True
    assert context.lifecycle_completion.tool_name == "agent_finish"
    assert accumulator.snapshot() == {
        "requests": 2,
        "input_tokens": 20,
        "output_tokens": 10,
        "total_tokens": 30,
    }
    assert len(requests) == 2
    assert all(str(request.url) == "https://gateway.invalid/v1/responses" for request in requests)
    assert all(request.headers["authorization"] == "Bearer fixture-key" for request in requests)
    for body in bodies:
        assert body["model"] == "openai/gpt-6-astra"
        assert body["parallel_tool_calls"] is False
        assert bool(body.get("stream")) is streaming
        assert body["tools"][0]["type"] == "function"
        assert "reasoning_effort" not in body
        if effort == "default":
            assert "reasoning" not in body
        else:
            assert body["reasoning"] == {"effort": effort}
    images = [item for item in bodies[1]["input"] if item.get("type") == "function_call_output"]
    assert images[0]["call_id"] == "call_0"
    assert images[0]["output"][0]["type"] == "input_image"
    assert images[0]["output"][0]["image_url"] == IMAGE_URL
    assert any(
        item.get("type") == "function_call_output" and item["output"] == images[0]["output"]
        for item in history
    )


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("effort", ["default", "none", "high"])
async def test_route_effort_applies_to_direct_helper_calls_without_mutating_settings(mode, streaming, effort):
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if mode == "responses":
            response = _response([_message("Fixture summary")])
            return _response_stream(response) if body.get("stream") else httpx.Response(200, json=response)
        completion = _completion({"text": "Fixture summary"}, 0)
        return _stream(completion) if body.get("stream") else httpx.Response(200, json=completion)

    request_settings = replace(
        model_settings(), reasoning=Reasoning(effort="medium"), max_tokens=100, extra_args={"timeout": 3}
    )
    original = copy.deepcopy(request_settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = make_platform_model(_settings(mode=mode, effort=effort), http_client=client)
        assert isinstance(
            model, PlatformResponsesModel if mode == "responses" else PlatformChatCompletionsModel
        )
        kwargs = {
            "system_instructions": None,
            "input": "Summarize fixture",
            "model_settings": request_settings,
            "tools": [],
            "output_schema": None,
            "handoffs": [],
            "tracing": ModelTracing.DISABLED,
            "previous_response_id": None,
            "conversation_id": None,
        }
        if streaming:
            async for _ in model.stream_response(**kwargs):
                pass
        else:
            await model.get_response(**kwargs)
    assert request_settings == original
    assert len(bodies) == 1
    body = bodies[0]
    field = "reasoning" if mode == "responses" else "reasoning_effort"
    if effort == "default":
        assert field not in body
    else:
        assert body[field] == ({"effort": effort} if mode == "responses" else effort)
    assert body["max_output_tokens" if mode == "responses" else "max_tokens"] == 100


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
async def test_provider_default_removes_real_sdk_gpt_model_default(mode):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        payload = _response([_message("Done")]) if mode == "responses" else _completion({"text": "Done"}, 0)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = make_platform_model(_settings("gpt-5.4", mode), http_client=client)
        agent = Agent(name="SDK defaults fixture", model=model)
        sdk_defaults = get_default_model_settings("gpt-5.4")
        assert sdk_defaults.reasoning.effort == "none"
        await Runner.run(
            agent, "Fixture", run_config=RunConfig(tracing_disabled=True, model_settings=sdk_defaults)
        )
    assert "reasoning" not in bodies[0]
    assert "reasoning_effort" not in bodies[0]
