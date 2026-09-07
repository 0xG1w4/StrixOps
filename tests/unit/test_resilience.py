"""Transient retry settings and usage tracking tests.

Session compaction and overflow behavior are covered in test_context_compaction.
"""

from __future__ import annotations

from strixops.engine.resilience import model_settings


def test_model_settings_carry_retry_and_usage():
    settings = model_settings()
    assert settings.retry is not None
    assert settings.retry.max_retries == 5
    assert settings.include_usage is True
    assert settings.parallel_tool_calls is False


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
