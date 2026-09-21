"""Latest main-conversation input size per agent, never cumulative token spend.

The SDK model boundary receives the resolved instructions, current input and
tool/schema definitions. Serialize those in full for a local estimate, then
replace that estimate with the provider's input usage when available. The
serialization is transient: only a count and fixed provenance fields leave
this wrapper. Images, provider framing and server-side state can make the
local estimate differ from actual provider token accounting.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from typing import Any

from agents import Model, ModelResponse, ModelSettings, ModelTracing, Tool
from agents.items import TResponseInputItem
from agents.models.openai_responses import Converter
from agents.usage import Usage
from pydantic import BaseModel

from strixops.engine.context_budget import count_tokens

logger = logging.getLogger(__name__)


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if is_dataclass(value) and not isinstance(value, type):
        # The fallback still retains complete declarative tool definitions,
        # while excluding executable callbacks that are not sent to a model.
        return {
            field.name: item for field in fields(value)
            if not field.name.startswith("_") and not callable(item := getattr(value, field.name))
        }
    return str(value)


def estimate_context_tokens(
    model: str,
    *,
    system_instructions: str | None,
    input: str | list[TResponseInputItem],
    tools: list[Tool],
    output_schema: Any,
    handoffs: list[Any],
    prompt: Any = None,
) -> int:
    """Estimate complete request input without the compactor's text truncation.

    Responses tool conversion also describes the function tools used on the
    Chat Completions route. This is a representation for counting, not a claim
    to reproduce either provider's precise wire framing or image tokenizer.
    """
    try:
        definitions = Converter.convert_tools(tools, handoffs, model=model).tools
    except Exception:  # noqa: BLE001 -- unfamiliar tool types must not block inference.
        definitions = [*tools, *handoffs]
    schema = None
    if output_schema is not None and not output_schema.is_plain_text():
        schema = {
            "name": output_schema.name(),
            "schema": output_schema.json_schema(),
            "strict": output_schema.is_strict_json_schema(),
        }
    serialized = json.dumps(
        {
            "instructions": system_instructions,
            "input": input,
            "tools_and_handoffs": definitions,
            "output_schema": schema,
            "prompt": prompt,
        },
        ensure_ascii=False, separators=(",", ":"), default=_json_default,
    )
    return max(0, count_tokens(model, serialized))


def _provider_input_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    # The SDK substitutes Usage() when a non-stream response omits usage.
    # That synthetic zero is not a measured empty request.
    if isinstance(usage, Usage) and usage.requests == 0:
        return None
    value = usage.get("input_tokens") if isinstance(usage, dict) else getattr(usage, "input_tokens", None)
    # Keep the provider's complete input count, including cached tokens.
    return value if type(value) is int and value >= 0 else None


class ContextUsageModel(Model):
    """Observe one agent's main requests while delegating inference unchanged.

    The loop installs this only on the Runner's agent clone. Compaction and
    secondary model calls continue using the original model, so their smaller
    prompts cannot overwrite the main conversation's current input size.
    """

    def __init__(
        self,
        inner: Model,
        *,
        agent_id: str,
        agent_name: str,
        on_update: Callable[[dict[str, Any]], None],
    ) -> None:
        self._inner = inner
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._on_update = on_update
        self._request_sequence = 0

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "")

    async def close(self) -> None:
        await self._inner.close()

    def get_retry_advice(self, request: Any) -> Any:
        return self._inner.get_retry_advice(request)

    def _publish(self, sequence: int, tokens: int, *, source: str, phase: str) -> None:
        if sequence != self._request_sequence:
            return
        snapshot = {
            "agent_name": self._agent_name,
            "model": self.model,
            "input_tokens": tokens,
            "source": source,
            "phase": phase,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        try:
            self._on_update(snapshot)
        except Exception as exc:  # noqa: BLE001 -- diagnostics never retry a completed model call.
            logger.warning("Context usage publication failed for %s (%s)", self._agent_id, type(exc).__name__)

    async def _start_request(self, **kwargs: Any) -> tuple[int, int | None]:
        self._request_sequence += 1
        sequence = self._request_sequence
        try:
            # Large history/tokenizer work must not stall other agents or Stop.
            estimated = await asyncio.to_thread(estimate_context_tokens, self.model, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- a counter failure must not block inference.
            logger.warning("Context input estimate failed for %s (%s)", self._agent_id, type(exc).__name__)
            return sequence, None
        self._publish(sequence, estimated, source="estimate", phase="request")
        return sequence, estimated

    def _complete_request(self, sequence: int, estimate: int | None, response: Any) -> None:
        tokens = _provider_input_tokens(response)
        if tokens is not None:
            self._publish(sequence, tokens, source="provider_usage", phase="response")
        elif estimate is not None:
            self._publish(sequence, estimate, source="estimate", phase="response")

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
        sequence, estimate = await self._start_request(
            system_instructions=system_instructions, input=input, tools=tools,
            output_schema=output_schema, handoffs=handoffs, prompt=prompt,
        )
        response = await self._inner.get_response(
            system_instructions, input, model_settings, tools, output_schema, handoffs, tracing,
            previous_response_id=previous_response_id, conversation_id=conversation_id, prompt=prompt,
        )
        self._complete_request(sequence, estimate, response)
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
        sequence, estimate = await self._start_request(
            system_instructions=system_instructions, input=input, tools=tools,
            output_schema=output_schema, handoffs=handoffs, prompt=prompt,
        )
        iterator = self._inner.stream_response(
            system_instructions, input, model_settings, tools, output_schema, handoffs, tracing,
            previous_response_id=previous_response_id, conversation_id=conversation_id, prompt=prompt,
        )
        failed = False
        try:
            async for event in iterator:
                if getattr(event, "type", "") == "response.completed":
                    self._complete_request(sequence, estimate, getattr(event, "response", None))
                yield event
        except BaseException:
            failed = True
            # The inner UsageTrackingModel attaches request-local retry
            # provenance. Preserve its original exception without rewriting it.
            raise
        finally:
            close = getattr(iterator, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    if not failed:
                        raise
                    logger.debug("Context-tracked model stream cleanup failed after a request error")
