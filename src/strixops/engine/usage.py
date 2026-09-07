"""Usage tracking model wrapper — run-level token accounting across agents.

Wraps the platform model; every completed model response's usage is
accumulated and reported through the run state (``llm_usage`` in run.json,
``usage.updated`` events for the console's live spend display). Works for
both streaming (reads usage off the terminal ``response.completed`` event)
and non-streaming paths.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

from agents import Model, ModelResponse, ModelSettings, ModelTracing, Tool
from agents.items import TResponseInputItem

logger = logging.getLogger(__name__)


class UsageAccumulator:
    """Thread-safe run totals, published after each completed response.

    RLock: ``add()`` snapshots the totals while holding the lock — a plain
    Lock deadlocked there on the very first model call (caught by the unit
    test before it ever reached a live scan).
    """

    def __init__(self, on_update: Callable[[dict[str, int]], None] | None = None) -> None:
        self._lock = threading.RLock()
        self._on_update = on_update
        self._published: dict[str, int] | None = None
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0

    def add(self, usage: _UsageLike | None) -> dict[str, int]:
        with self._lock:
            self.requests += 1
            if usage is not None:
                self.input_tokens += usage.input_tokens or 0
                self.output_tokens += usage.output_tokens or 0
                self.total_tokens += usage.total_tokens or 0
            return self.flush()

    def flush(self) -> dict[str, int]:
        """Publish pending totals without counting a response a second time."""
        with self._lock:
            totals = self.snapshot()
            if self._on_update is not None and self.requests and totals != self._published:
                try:
                    self._on_update(dict(totals))
                except Exception:
                    # Keep the totals for the next response or final flush.
                    # A storage failure must not retry an already completed LLM call.
                    logger.exception("Failed to persist model usage")
                else:
                    self._published = dict(totals)
            return totals

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "requests": self.requests,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
            }


class _UsageLike:
    """Duck-typed usage view — the SDK's Usage and openai's ResponseUsage
    both carry the same token fields but share no common type."""

    def __init__(self, source: Any) -> None:
        self.input_tokens = int(getattr(source, "input_tokens", 0) or 0)
        self.output_tokens = int(getattr(source, "output_tokens", 0) or 0)
        self.total_tokens = int(getattr(source, "total_tokens", 0) or 0)


def _usage_from_response(response: Any) -> _UsageLike | None:
    usage = getattr(response, "usage", None)
    return _UsageLike(usage) if usage is not None else None


def _usage_from_stream_event(event: Any) -> _UsageLike | None:
    """Pull usage off a raw stream event's terminal payload, if present."""
    if getattr(event, "type", "") != "response.completed":
        return None
    response = getattr(event, "response", None)
    usage = getattr(response, "usage", None)
    return _UsageLike(usage) if usage is not None else None


class UsageTrackingModel(Model):
    """Delegate model that records per-response token usage."""

    def __init__(self, inner: Model, accumulator: UsageAccumulator) -> None:
        self._inner = inner
        self.accumulator = accumulator

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "")

    async def close(self) -> None:
        await self._inner.close()

    def get_retry_advice(self, request: Any) -> Any:
        return self._inner.get_retry_advice(request)

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Any,
        handoffs: list[Any],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any = None,
    ) -> ModelResponse:
        response = await self._inner.get_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )
        self.accumulator.add(_usage_from_response(response))
        return response

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Any,
        handoffs: list[Any],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any = None,
    ) -> AsyncIterator[Any]:
        # Pass the stream through untouched — the user's gateway already
        # works with real streaming — and read usage off the terminal event
        # (present with include_usage=True in model settings).
        iterator = self._inner.stream_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )
        async for event in iterator:
            if getattr(event, "type", "") == "response.completed":
                self.accumulator.add(_usage_from_stream_event(event))
            yield event
