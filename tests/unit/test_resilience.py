"""Transient retry settings and usage tracking tests.

Session compaction and overflow behavior are covered in test_context_compaction.
"""

from __future__ import annotations

import httpx
import pytest
from agents.retry import ModelRetryAdvice, ModelRetryNormalizedError, RetryPolicyContext
from openai import APIError, InternalServerError

from strixops.engine.resilience import model_settings
from tests.unit.test_model_errors import azure_policy_message


def test_model_settings_carry_retry_and_usage():
    settings = model_settings()
    assert settings.retry is not None
    assert settings.retry.max_retries == 5
    assert settings.include_usage is True
    assert settings.parallel_tool_calls is False


@pytest.mark.parametrize("http_error", [False, True])
async def test_explicit_policy_vetoes_provider_retry_advice_even_wrapped_as_500(http_error):
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")
    body = {"code": 500, "message": azure_policy_message()}
    if http_error:
        error = InternalServerError("failed", response=httpx.Response(500, request=request), body=body)
    else:
        error = APIError("failed", request=request, body=body)
    context = RetryPolicyContext(
        error=error,
        attempt=1,
        max_retries=5,
        stream=True,
        normalized=ModelRetryNormalizedError(status_code=500),
        provider_advice=ModelRetryAdvice(suggested=True, replay_safety="safe"),
    )
    decision = await model_settings().retry.policy(context)
    assert decision.retry is False


def test_usage_accumulator_and_tracking_model():
    import asyncio

    from agents import Model, ModelResponse, ModelSettings, ModelTracing
    from agents.items import Usage

    from strixops.engine.usage import UsageAccumulator, UsageTrackingModel

    class _Fake(Model):
        @property
        def model(self) -> str:
            return "fake"

        async def close(self) -> None:
            return None

        async def get_response(self, *args, **kwargs) -> ModelResponse:
            return ModelResponse(
                output=[],
                response_id="resp-test",
                usage=Usage(input_tokens=100, output_tokens=20, total_tokens=120),
            )

        async def stream_response(self, *args, **kwargs):
            yield {"type": "unused"}

    accumulator = UsageAccumulator()
    tracked = UsageTrackingModel(_Fake(), accumulator)

    async def scenario():
        await tracked.get_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
        )
        await tracked.get_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
        )

    asyncio.run(scenario())
    snapshot = accumulator.snapshot()
    assert snapshot == {
        "requests": 2,
        "input_tokens": 200,
        "output_tokens": 40,
        "total_tokens": 240,
    }
