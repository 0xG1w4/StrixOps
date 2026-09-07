"""Responses transport retains SDK-native function outputs, including images."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

import httpx
from agents.models.openai_responses import OpenAIResponsesModel
from agents.retry import ModelRetryAdvice, ModelRetryAdviceRequest
from openai import APIError
from openai.types.responses import ResponseStreamEvent

from strixops.config.reasoning_transport import RouteReasoningMixin

# Match the SDK runner's replay guard for diagnostics only. No event is filtered
# or reclassified here, and the SDK still owns every retry decision.
_RETRY_SAFE_STREAM_EVENT_TYPES = frozenset({"response.created", "response.in_progress"})


def _gateway_request_id(stream: object) -> str | None:
    """Read one gateway correlation header from the existing HTTP stream.

    Agents SDK wraps OpenAI's AsyncStream in its request-ID iterator. Inspect
    that bounded wrapper chain without modifying the shared client or headers.
    Custom streams without this shape simply have no gateway request ID.
    """
    for _ in range(3):
        response = getattr(stream, "response", None)
        if isinstance(response, httpx.Response):
            return response.headers.get("x-litellm-call-id")
        stream = getattr(stream, "_stream", None)
        if stream is None:
            break
    return None


class _RequestIdStream:
    """Keep bounded diagnostics on errors raised while decoding an SSE stream.

    The OpenAI SDK keeps an in-stream error's body but does not copy the HTTP
    request ID to that error. The Agents SDK wrapper already has the ID, so
    preserve it without replacing the error or changing stream cleanup.
    """

    def __init__(self, stream: AsyncIterator[ResponseStreamEvent]) -> None:
        self._stream = stream
        self.request_id = getattr(stream, "request_id", None)
        self.gateway_request_id = _gateway_request_id(stream)
        self._event_count = 0
        self._retry_blocked_by: str | None = None

    def __aiter__(self) -> _RequestIdStream:
        return self

    async def __anext__(self) -> ResponseStreamEvent:
        try:
            event = await anext(self._stream)
        except APIError as exc:
            if self.request_id and not getattr(exc, "request_id", None):
                exc.request_id = self.request_id
            if self.gateway_request_id:
                exc._strixops_gateway_request_id = self.gateway_request_id
            exc._strixops_stream_event_count = self._event_count
            if self._retry_blocked_by is not None:
                exc._strixops_stream_retry_blocked_by = self._retry_blocked_by
            raise
        self._event_count += 1
        event_type = getattr(event, "type", None)
        event_type = event_type if isinstance(event_type, str) else "unknown"
        if self._retry_blocked_by is None and event_type not in _RETRY_SAFE_STREAM_EVENT_TYPES:
            self._retry_blocked_by = event_type
        return event

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None) or getattr(self._stream, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    async def close(self) -> None:
        await self.aclose()


class PlatformResponsesModel(RouteReasoningMixin, OpenAIResponsesModel):
    """Responses model with route reasoning shared by agents and helper calls."""

    async def _fetch_response(self, *args: Any, **kwargs: Any) -> Any:
        response = await super()._fetch_response(*args, **kwargs)
        return _RequestIdStream(response) if hasattr(response, "__aiter__") else response

    def get_retry_advice(self, request: ModelRetryAdviceRequest) -> ModelRetryAdvice | None:
        # This is the SDK's physical model-request attempt, distinct from the
        # engine's lifecycle cycles. Delegation preserves the SDK replay guard.
        if isinstance(request.error, APIError):
            request.error._strixops_model_request_attempt = request.attempt
        return super().get_retry_advice(request)
