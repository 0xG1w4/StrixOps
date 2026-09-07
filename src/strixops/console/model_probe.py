"""Test a draft route with a bounded, harmless streamed function-tool round trip."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

import httpx
from agents import Model, ModelResponse, ModelSettings, ModelTracing, function_tool
from agents.items import Usage
from fastapi import HTTPException
from httpx import AsyncClient
from openai import APIConnectionError, APIStatusError, APITimeoutError

from strixops.config.model_options import resolved_api_mode, validate_model_options
from strixops.config.provider import make_platform_model, strip_provider_prefix
from strixops.config.settings import EngineSettings
from strixops.console.model_catalog import _connection, _error

PROBE_TIMEOUT = 45
PROBE_OUTPUT_TOKENS = 2048


async def _round_trip(model: Model, api_mode: str) -> None:
    receipt = secrets.token_hex(12)

    @function_tool
    def route_probe() -> str:
        """Return a receipt to verify that a model can read a function result."""
        return receipt

    instructions = (
        "This is a model connection test. Call route_probe once with no arguments. "
        "After receiving the tool result, reply with exactly the receipt it returned, "
        "without punctuation or explanation."
    )
    inputs: list[Any] = [{"role": "user", "content": "Test this model connection."}]

    async def turn(tool_choice: str) -> ModelResponse:
        completed = None
        async for event in model.stream_response(
            system_instructions=instructions,
            input=inputs,
            model_settings=ModelSettings(
                tool_choice=tool_choice,
                parallel_tool_calls=False,
                include_usage=True,
                max_tokens=PROBE_OUTPUT_TOKENS,
                store=False,
                response_include=["reasoning.encrypted_content"] if api_mode == "responses" else None,
            ),
            tools=[route_probe],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ):
            if event.type == "response.completed":
                completed = event.response
        if completed is None or completed.status not in {None, "completed"}:
            raise _error("probe_incomplete", "The model did not complete its streamed response.", 502)
        return ModelResponse(output=completed.output, usage=Usage(), response_id=completed.id)

    first = await turn("route_probe")
    calls = [item for item in first.output if item.type == "function_call"]
    if len(calls) != 1 or calls[0].name != "route_probe":
        raise _error("probe_tool_call", "The model did not return the requested test function call.", 502)
    try:
        arguments = json.loads(calls[0].arguments)
    except (ValueError, TypeError) as exc:
        raise _error("probe_tool_call", "The model returned invalid test function arguments.", 502) from exc
    if arguments != {}:
        raise _error("probe_tool_call", "The test function must be called with no arguments.", 502)
    inputs.extend(first.to_input_items())
    inputs.append({"type": "function_call_output", "call_id": calls[0].call_id, "output": receipt})
    second = await turn("none")
    text = "".join(
        part.text
        for item in second.output
        if item.type == "message"
        for part in item.content
        if part.type == "output_text"
    ).strip()
    if text != receipt or any(item.type == "function_call" for item in second.output):
        raise _error("probe_tool_result", "The model did not correctly read the test function result.", 502)


async def test_model(
    *,
    route_type: str,
    llm_api_base: str,
    llm_api_key: str,
    model: str,
    api_mode: str = "auto",
    reasoning_effort: str = "default",
    profile_id: str | None = None,
) -> dict[str, Any]:
    """No profile writes or scan tools; only the selected provider receives a test prompt."""
    model = strip_provider_prefix(model)
    if not model:
        raise _error("model_required", "Choose a model to test.")
    errors = validate_model_options(model, api_mode, reasoning_effort)
    if errors:
        raise _error("invalid_model_options", " ".join(errors))
    base, key = _connection(
        route_type=route_type, llm_api_base=llm_api_base, llm_api_key=llm_api_key, profile_id=profile_id
    )
    mode = resolved_api_mode(model, api_mode)
    settings = EngineSettings(
        llm_api_base=base,
        llm_api_key=key,
        strix_llm=model,
        strix_runs="",
        operator_hints_dir="",
        host_workspace_dir="",
        dry_run=False,
        llm_api_mode=api_mode,
        llm_reasoning_effort=reasoning_effort,
    )
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with AsyncClient(
                timeout=PROBE_TIMEOUT, follow_redirects=False, trust_env=False
            ) as http_client:
                platform_model = make_platform_model(settings, http_client=http_client)
                try:
                    await _round_trip(platform_model, mode)
                finally:
                    await platform_model.close()
    except (TimeoutError, APITimeoutError) as exc:
        raise _error("upstream_timeout", "The model test did not finish within 45 seconds.", 504) from exc
    except APIStatusError as exc:
        status = exc.status_code
        if status in {401, 403}:
            raise _error("upstream_auth", "The provider rejected this API key or model access.", 502) from exc
        if 300 <= status < 400:
            raise _error(
                "upstream_redirect", "Enter the provider's direct API base; redirects are disabled.", 502
            ) from exc
        if status in {400, 404, 422}:
            raise _error(
                "upstream_incompatible",
                f"The provider returned HTTP {status}. Check that this model route supports "
                f"{mode}, the selected effort, streaming, and function tools.",
                502,
            ) from exc
        raise _error(
            "upstream_http", f"The provider returned HTTP {status} during the model test.", 502
        ) from exc
    except (APIConnectionError, httpx.HTTPError) as exc:
        raise _error("upstream_connection", "Could not connect to the model provider.", 502) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # Provider errors may echo request headers or prompts. Return only a local explanation.
        raise _error(
            "probe_failed", "The provider returned an unsupported or invalid model response.", 502
        ) from exc
    return {
        "ok": True,
        "model": model,
        "api_mode": mode,
        "reasoning_effort": reasoning_effort,
        "message": "Streaming and the function-tool round trip succeeded.",
    }
