"""Apply route reasoning after SDK defaults, on every model request."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agents.items import TResponseInputItem
from agents.model_settings import ModelSettings
from openai.types.shared.reasoning import Reasoning


class RouteReasoningMixin:
    """Keep SDK request/stream/retry handling while enforcing the saved effort.

    This boundary covers Runner calls and direct summary/deduplication calls.
    Replacing the fully resolved settings also makes ``default`` omit the wire
    field when the SDK has already inserted its own model-specific default.
    """

    def __init__(self, *args: Any, reasoning_effort: str = "default", **kwargs: Any) -> None:
        self.reasoning_effort = reasoning_effort
        super().__init__(*args, **kwargs)

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        reasoning = None if self.reasoning_effort == "default" else Reasoning(effort=self.reasoning_effort)
        configured = replace(model_settings, reasoning=reasoning)
        return await super()._fetch_response(system_instructions, input, configured, *args, **kwargs)
