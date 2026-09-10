"""Content-free model stream measurements for the MCP request-test workbench."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from typing import Any

from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import OpenAIResponsesModel

from strixops.engine.stream_cleanup import join_task


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, default=str)) if value is not None else 0


class StreamMetrics:
    def __init__(self, inputs: Any, system: Any, known_tools: set[str], *, raw_chat: bool = False) -> None:
        self.known_tools = known_tools
        self.raw_chat = raw_chat
        self.data: dict[str, Any] = {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "input_chars": _chars(inputs),
            "system_chars": _chars(system),
            "first_event_seconds": None,
            "first_output_seconds": None,
            "finish_reason": None,
            "truncated": False,
            "tools": [],
        }

    def observe(self, event: Any, elapsed: float) -> str | None:
        """Inspect event shape and counters; never retain generated content or arguments."""
        activity = None
        if self.data["first_event_seconds"] is None:
            self.data["first_event_seconds"] = round(elapsed, 3)
            activity = "streaming"
        kind = _get(event, "type", "")
        item = _get(event, "item")
        output = _get(_get(event, "response"), "output", []) or []
        items = [item] if item is not None else output
        has_output = bool(_get(event, "delta"))
        output_kind = (
            "reasoning" if "reasoning" in kind else "tool_call" if "function_call" in kind else "text"
        )
        for current in items:
            item_type = _get(current, "type", "")
            if item_type == "function_call":
                output_kind = "tool_call"
                name = _get(current, "name")
                if isinstance(name, str) and name in self.known_tools and name not in self.data["tools"]:
                    self.data["tools"].append(name)
                has_output |= bool(_get(current, "arguments"))
            elif item_type == "message":
                has_output |= any(
                    bool(_get(part, "text") or _get(part, "refusal"))
                    for part in (_get(current, "content", []) or [])
                )
            elif item_type == "reasoning":
                output_kind = "reasoning"
                has_output |= any(
                    bool(_get(part, "text"))
                    for part in ((_get(current, "summary", []) or []) + (_get(current, "content", []) or []))
                )
        if has_output and self.data["first_output_seconds"] is None:
            self.data["first_output_seconds"] = round(elapsed, 3)
            activity = output_kind
        response = _get(event, "response")
        usage = _get(response, "usage")
        if usage is not None and not self.raw_chat:
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                self.data[field] = _count(_get(usage, field))
            self.data["cached_input_tokens"] = _count(
                _get(_get(usage, "input_tokens_details"), "cached_tokens")
            )
            self.data["reasoning_tokens"] = _count(
                _get(_get(usage, "output_tokens_details"), "reasoning_tokens")
            )
        finish = _get(response, "finish_reason") or _get(_get(response, "incomplete_details"), "reason")
        if finish == "max_output_tokens":
            finish = "length"
        if isinstance(finish, str) and finish in {
            "completed",
            "stop",
            "tool_calls",
            "length",
            "content_filter",
            "incomplete",
            "failed",
            "cancelled",
        }:
            self.data["finish_reason"] = finish
            self.data["truncated"] = finish == "length"
        elif _get(response, "status") in {"incomplete", "failed", "cancelled"}:
            self.data["finish_reason"] = _get(response, "status")
        return activity

    def observe_chat(self, chunk: Any) -> None:
        """Read provider counters before the SDK fills absent usage details with zero."""
        usage = _get(chunk, "usage")
        if usage is not None:
            for target, source in (
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
                ("total_tokens", "total_tokens"),
            ):
                self.data[target] = _count(_get(usage, source))
            self.data["cached_input_tokens"] = _count(
                _get(_get(usage, "prompt_tokens_details"), "cached_tokens")
            )
            self.data["reasoning_tokens"] = _count(
                _get(_get(usage, "completion_tokens_details"), "reasoning_tokens")
            )
        choices = _get(chunk, "choices", []) or []
        choice = next((item for item in choices if _get(item, "index") == 0), None)
        finish = _get(choice, "finish_reason")
        if isinstance(finish, str) and finish in {"stop", "tool_calls", "length", "content_filter"}:
            self.data["finish_reason"] = finish
            self.data["truncated"] = finish == "length"


class _ObservedProviderStream:
    """Forward original chunks, and join one idempotent underlying stream close."""

    def __init__(self, stream: Any, observe: Callable[[Any], None]) -> None:
        self.stream = stream
        self.observe = observe
        self.close_task: asyncio.Task | None = None

    def __getattr__(self, name: str) -> Any:
        # Preserve the SDK's request-ID/retry diagnostic wrapper attributes.
        return getattr(self.stream, name)

    def __aiter__(self) -> _ObservedProviderStream:
        return self

    async def __anext__(self) -> Any:
        chunk = await anext(self.stream)
        self.observe(chunk)
        return chunk

    async def aclose(self) -> None:
        async def close() -> None:
            closer = getattr(self.stream, "aclose", None) or getattr(self.stream, "close", None)
            if callable(closer):
                result = closer()
                if inspect.isawaitable(result):
                    await result

        if self.close_task is None:
            self.close_task = asyncio.create_task(close(), name="mcp-model-stream-close")
        if await join_task(self.close_task):
            raise asyncio.CancelledError


def attach_stream_observer(
    model: Any, metrics: Callable[[], StreamMetrics | None], streams: list[Any]
) -> bool:
    """Observe only this job's model instance, retaining provider routing/retry behavior.

    The pinned Chat adapter otherwise discards finish_reason and synthesizes missing
    usage-detail zeroes. Responses events already retain their provider fields.
    """
    raw_chat = isinstance(model, OpenAIChatCompletionsModel)
    if not raw_chat and not isinstance(model, OpenAIResponsesModel):
        return False
    fetch = model._fetch_response

    async def observed_fetch(*args: Any, **kwargs: Any) -> Any:
        result = await fetch(*args, **kwargs)
        if raw_chat:
            if not isinstance(result, tuple) or len(result) != 2:
                return result
            response, stream = result
        else:
            if not hasattr(result, "__aiter__"):
                return result
            stream = result

        current = metrics()

        def observe(chunk: Any) -> None:
            if raw_chat and current is not None:
                current.observe_chat(chunk)

        observed = _ObservedProviderStream(stream, observe)
        streams.append(observed)
        return (response, observed) if raw_chat else observed

    model._fetch_response = observed_fetch
    return raw_chat
