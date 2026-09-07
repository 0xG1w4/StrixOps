"""Keep useful provider identifiers while excluding credentials and raw bodies."""

import json

import httpx
import pytest
from openai import APIError, BadRequestError

from strixops.config.model_errors import format_model_error, model_error_details, upstream_policy_code


def azure_policy_message():
    return "AzureException BadRequestError - " + json.dumps({
        "error": {
            "code": "cyber_policy", "type": "invalid_request",
            "message": "This content was flagged for possible cybersecurity risk. PRIVATE_PROVIDER_TEXT",
        }
    }) + " Available Model Group Fallbacks: PRIVATE_FALLBACK_LIST"


def test_stream_error_keeps_structured_diagnostics_and_redacts_request_credentials():
    request = httpx.Request(
        "POST", "https://provider.invalid/v1/responses",
        headers={"authorization": "Bearer private-fixture-key"},
    )
    exc = APIError(
        "litellm.APIError: Response API in-stream error; echo private-fixture-key",
        request=request,
        body={
            "code": "503", "type": "service_unavailable", "param": "tools[0].parameters",
            "message": "RAW_PRIVATE_PROMPT", "headers": {"authorization": "private-fixture-key"},
        },
    )
    exc.request_id = "req-fixture-123"
    details = model_error_details(exc)
    assert "api=responses" in details and "code=503" in details
    assert "param=tools[0].parameters" in details and "request_id=req-fixture-123" in details
    rendered = format_model_error(exc)
    assert "Response API in-stream error" in rendered
    assert "private-fixture-key" not in rendered and "RAW_PRIVATE_PROMPT" not in rendered


def test_error_diagnostics_ignore_unstructured_or_secret_values():
    exc = APIError(
        "failed", request=httpx.Request("POST", "https://provider.invalid/v1/responses"),
        body={"code": {"secret": "value"}, "type": "Bearer secret", "param": "sk-private-key",
              "request_id": "line1\nline2"},
    )
    assert model_error_details(exc) == "api=responses"


def test_nested_error_and_http_status_are_preserved_without_provider_payloads():
    exc = BadRequestError(
        "Invalid request", response=httpx.Response(
            400, request=httpx.Request("POST", "https://provider.invalid/v1/responses"),
            headers={"x-request-id": "req-http-fixture"},
        ), body={"error": {"code": "unsupported_parameter", "param": "reasoning.effort"}},
    )
    text = format_model_error(exc)
    assert "status=400" in text and "code=unsupported_parameter" in text
    assert "param=reasoning.effort" in text and "request_id=req-http-fixture" in text


def test_non_api_exception_format_is_unchanged():
    assert format_model_error(ValueError("invalid tool")) == "ValueError: invalid tool"


def test_stream_diagnostic_tokens_are_bounded_and_exclude_request_secrets():
    exc = APIError(
        "failed", request=httpx.Request(
            "POST", "https://provider.invalid/v1/responses",
            headers={"authorization": "Bearer fixture-private-key"},
        ), body={},
    )
    exc._strixops_gateway_request_id = "fixture-private-key"
    exc._strixops_model_request_attempt = 6
    exc._strixops_stream_event_count = 0
    exc._strixops_stream_retry_blocked_by = "response." + "x" * 161
    details = model_error_details(exc)
    assert "model_request_attempt=6" in details and "stream_events=0" in details
    assert "gateway_request_id=" not in details and "retry_blocked_by=" not in details
    exc._strixops_gateway_request_id = "call-fixture\nPRIVATE"
    exc._strixops_stream_retry_blocked_by = {"payload": "PRIVATE"}
    assert "PRIVATE" not in model_error_details(exc)


@pytest.mark.parametrize("body", [
    {"code": "cyber_policy"},
    {"error": {"innererror": {"code": "cyber_policy"}}},
    {"code": 500, "error": {"message": azure_policy_message()}},
    {"code": 500, "message": json.dumps({"error": {"message": azure_policy_message()}})},
    json.dumps({"error": {"code": "cyber_policy"}}),
    {"error": json.dumps({"code": "cyber_policy"})},
])
def test_explicit_policy_is_identified_in_structured_and_embedded_errors(body):
    exc = APIError(
        "Gateway failed PRIVATE_FALLBACK_LIST",
        request=httpx.Request("POST", "https://provider.invalid/v1/responses"), body=body,
    )
    assert upstream_policy_code(exc) == "cyber_policy"
    rendered = format_model_error(exc)
    assert "blocked this request under its cybersecurity policy" in rendered
    assert "upstream_policy=cyber_policy" in rendered
    assert "PRIVATE" not in rendered


def test_azure_json_error_in_exception_message_is_recognized_through_wrapper():
    exc = APIError(
        azure_policy_message(),
        request=httpx.Request("POST", "https://provider.invalid/v1/responses"), body=None,
    )
    wrapper = RuntimeError("PRIVATE_WRAPPER_TEXT")
    wrapper.__cause__ = exc
    assert upstream_policy_code(wrapper) == "cyber_policy"
    assert "PRIVATE" not in format_model_error(wrapper)
    assert "cyber_policy" in format_model_error(wrapper)


@pytest.mark.parametrize("body", [
    {"code": 500},
    {"message": "litellm.APIError: Response API in-stream error", "code": "500"},
    {"code": "cyber_policy_other"},
    {"message": "The request includes the word cyber_policy."},
    {"message": "Possible cybersecurity risk"},
    {"debug": {"code": "cyber_policy"}},
    {"input": {"error": {"code": "cyber_policy"}}},
    {"message": '{"code": cyber_policy}'},
])
def test_policy_is_not_inferred_from_generic_errors_or_unrelated_payloads(body):
    exc = APIError(
        "litellm.APIError: Response API in-stream error",
        request=httpx.Request("POST", "https://provider.invalid/v1/responses"), body=body,
    )
    assert upstream_policy_code(exc) is None
    assert "blocked this request" not in format_model_error(exc)
    assert "upstream_policy=" not in model_error_details(exc)


def test_policy_parser_stops_on_cycles_and_bounded_invalid_json():
    body = {"code": 500}
    body["error"] = body
    body["message"] = "{" * 100000
    exc = APIError("failed", request=httpx.Request("POST", "https://provider.invalid"), body=body)
    exc.__cause__ = exc
    assert upstream_policy_code(exc) is None
