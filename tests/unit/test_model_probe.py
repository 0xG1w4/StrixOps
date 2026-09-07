"""Exercise the draft model test through real SDK HTTP with no external requests."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from strixops.console import model_probe, server, settings_store
from strixops.testing.scripted_gateway import _completion
from tests.unit.test_model_errors import azure_policy_message
from tests.unit.test_vision_transport import _stream


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    path = tmp_path / "console.json"
    monkeypatch.setenv("STRIXOPS_CONSOLE_CONFIG", str(path))
    return path


def request_body(**overrides):
    return {
        "route_type": "custom",
        "llm_api_base": "https://model.invalid/v1",
        "llm_api_key": "test-private-key",
        "model": "gateway-alias",
        "api_mode": "chat_completions",
        "reasoning_effort": "default",
        **overrides,
    }


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    options = []

    def create(**kwargs):
        options.append(kwargs.copy())
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(model_probe, "AsyncClient", create)
    return options


def responses_stream(output, model):
    event = {
        "type": "response.completed",
        "sequence_number": 0,
        "response": {
            "id": "resp-probe",
            "created_at": 0,
            "object": "response",
            "model": model,
            "status": "completed",
            "output": output,
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        },
    }
    return httpx.Response(
        200,
        text=f"event: response.completed\ndata: {json.dumps(event)}\n\n",
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.parametrize(
    ("model", "mode", "effort"),
    [
        ("gateway-alias", "chat_completions", "default"),
        ("gateway-alias", "responses", "high"),
        ("gpt-6-astra", "chat_completions", "high"),
        ("gpt-5.5", "chat_completions", "default"),
    ],
)
def test_probe_verifies_streamed_tool_result_without_writing_profile(
    monkeypatch, isolated_settings, model, mode, effort,
):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append((request, body))
        assert body["stream"] is True
        assert body["store"] is False
        assert body["model"] == model
        assert "test-private-key" not in json.dumps(body)
        if mode == "chat_completions":
            assert request.url.path == "/v1/chat/completions"
            if effort == "default":
                assert "reasoning_effort" not in body
            else:
                assert body["reasoning_effort"] == effort
            if len(requests) == 1:
                return _stream(_completion({"tool_calls": [{"name": "route_probe"}]}, 0))
            receipt = next(item["content"] for item in body["messages"] if item["role"] == "tool")
            return _stream(_completion({"text": receipt}, 1))
        assert request.url.path == "/v1/responses"
        assert body["reasoning"] == {"effort": "high"}
        assert "reasoning.encrypted_content" in body["include"]
        if len(requests) == 1:
            output = [
                {
                    "id": "fc-probe",
                    "type": "function_call",
                    "call_id": "call-probe",
                    "name": "route_probe",
                    "arguments": "{}",
                    "status": "completed",
                }
            ]
        else:
            receipt = next(
                item["output"] for item in body["input"] if item.get("type") == "function_call_output"
            )
            output = [
                {
                    "id": "msg-probe",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": receipt, "annotations": []}],
                }
            ]
        return responses_stream(output, body["model"])

    options = mock_http(monkeypatch, respond)
    body = request_body(model=model, api_mode=mode, reasoning_effort=effort)
    with TestClient(server.app) as client:
        result = client.post("/api/settings/test-model", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["ok"] is True
    assert result.json()["api_mode"] == mode
    assert len(requests) == 2
    assert all(request.headers["authorization"] == "Bearer test-private-key" for request, _ in requests)
    assert all(option["follow_redirects"] is False and option["trust_env"] is False for option in options)
    assert "test-private-key" not in result.text
    assert not isolated_settings.exists()


@pytest.mark.parametrize("failure", ["no_tool", "wrong_tool", "invalid_arguments", "wrong_receipt"])
@pytest.mark.asyncio
async def test_success_requires_correct_function_and_result(monkeypatch, failure):
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "no_tool":
                return _stream(_completion({"text": "I can call tools."}, 0))
            turn = {"tool_calls": [{"name": "wrong" if failure == "wrong_tool" else "route_probe"}]}
            completion = _completion(turn, 0)
            if failure == "invalid_arguments":
                completion["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "bad json"
            return _stream(completion)
        return _stream(_completion({"text": "I received the result."}, 1))

    mock_http(monkeypatch, respond)
    with pytest.raises(HTTPException) as caught:
        await model_probe.test_model(**request_body())
    assert caught.value.detail["code"] in {"probe_tool_call", "probe_tool_result"}
    assert calls == (2 if failure == "wrong_receipt" else 1)


@pytest.mark.parametrize("status", [307, 401, 400, 404, 422])
@pytest.mark.asyncio
async def test_upstream_errors_are_sanitized_and_redirects_disabled(monkeypatch, status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={"error": {"message": "echo test-private-key"}},
            headers={"location": "https://different.invalid/v1"},
        )

    mock_http(monkeypatch, respond)
    with pytest.raises(HTTPException) as caught:
        await model_probe.test_model(**request_body())
    assert "test-private-key" not in json.dumps(caught.value.detail)
    assert len(requests) == 1
    assert caught.value.detail["code"] in {"upstream_redirect", "upstream_auth", "upstream_incompatible"}


@pytest.mark.asyncio
async def test_saved_key_is_bound_to_source_endpoint_before_probe(monkeypatch):
    settings_store.save_settings(
        {
            "profiles": [
                {
                    "id": "source",
                    "route_type": "custom",
                    "llm_api_base": "https://model.invalid/v1",
                    "llm_api_key": "test-saved-private",
                }
            ]
        }
    )
    requests = []
    mock_http(monkeypatch, lambda request: requests.append(request))
    with pytest.raises(HTTPException) as caught:
        await model_probe.test_model(
            **request_body(
                profile_id="source", llm_api_key="•••vate", llm_api_base="https://other.invalid/v1"
            )
        )
    assert caught.value.detail["code"] == "endpoint_changed"
    assert not requests


@pytest.mark.asyncio
async def test_model_options_fail_before_any_http(monkeypatch):
    requests = []
    mock_http(monkeypatch, lambda request: requests.append(request))
    with pytest.raises(HTTPException) as caught:
        await model_probe.test_model(**request_body(model="gpt-6-astra", api_mode="invalid"))
    assert caught.value.detail["code"] == "invalid_model_options"
    assert not requests


def test_responses_stream_error_returns_safe_diagnostics(monkeypatch):
    def respond(request):
        error = {"error": {
            "message": "litellm.APIError: Response API in-stream error test-private-key",
            "code": "server_error", "type": "api_error", "param": "tools",
            "debug": "PRIVATE_PROMPT_MUST_NOT_LEAK",
        }}
        return httpx.Response(
            200, text=f"event: error\ndata: {json.dumps(error)}\n\n",
            headers={"content-type": "text/event-stream", "x-request-id": "req-probe-fixture"},
        )

    mock_http(monkeypatch, respond)
    with TestClient(server.app) as client:
        response = client.post(
            "/api/settings/test-model", json=request_body(api_mode="responses"),
        )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "upstream_stream_error"
    assert "api=responses" in detail["diagnostics"]
    assert "code=server_error" in detail["diagnostics"]
    assert "request_id=req-probe-fixture" in detail["diagnostics"]
    assert "test-private-key" not in response.text
    assert "PRIVATE_PROMPT_MUST_NOT_LEAK" not in response.text


@pytest.mark.parametrize("stream_error", [False, True])
def test_explicit_provider_policy_is_distinct_from_route_incompatibility(monkeypatch, stream_error):
    requests = []

    def respond(request):
        requests.append(request)
        error = {"error": {"code": 500 if stream_error else 400, "message": azure_policy_message()}}
        if stream_error:
            return httpx.Response(
                200, text=f"event: error\ndata: {json.dumps(error)}\n\n",
                headers={"content-type": "text/event-stream", "x-litellm-call-id": "call-policy-fixture"},
            )
        return httpx.Response(400, json=error)

    mock_http(monkeypatch, respond)
    with TestClient(server.app) as client:
        response = client.post("/api/settings/test-model", json=request_body(api_mode="responses"))
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "upstream_policy"
    assert "cybersecurity policy" in detail["message"]
    assert "upstream_policy=cyber_policy" in detail["diagnostics"]
    assert "PRIVATE" not in response.text
    assert len(requests) == 1


def test_generic_500_stream_error_is_not_labelled_as_policy(monkeypatch):
    def respond(request):
        error = {"error": {"code": 500, "message": "litellm.APIError: Response API in-stream error"}}
        return httpx.Response(
            200, text=f"event: error\ndata: {json.dumps(error)}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    mock_http(monkeypatch, respond)
    with TestClient(server.app) as client:
        response = client.post("/api/settings/test-model", json=request_body(api_mode="responses"))
    detail = response.json()["detail"]
    assert detail["code"] == "upstream_stream_error"
    assert "upstream_policy=" not in detail["diagnostics"]
