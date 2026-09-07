"""Keep sandbox image results on the existing Chat Completions route.

Strix's LiteLLM model preserves image content in function outputs. The pinned
SDK's direct OpenAI model retains only their text. Chat Completions accepts
images in user messages, so move each image into an adjacent user message
while retaining its paired tool result. This is only a request projection;
the canonical session still holds the structured image tool output.
"""

from __future__ import annotations

from typing import Any

from agents.items import TResponseInputItem
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

from strixops.config.reasoning_transport import RouteReasoningMixin


def project_tool_images(input: str | list[TResponseInputItem]) -> str | list[TResponseInputItem]:
    """Preserve images without interrupting a parallel tool-result group."""
    if isinstance(input, str):
        return input

    projected: list[Any] = []
    image_messages: list[Any] = []
    pending_calls: set[str] = set()
    for item in input:
        if not isinstance(item, dict):
            projected.append(item)
            continue
        item_type = item.get("type")
        call_id = item.get("call_id")
        if item_type == "function_call" and isinstance(call_id, str):
            pending_calls.add(call_id)
        if item_type == "function_call_output":
            output = item.get("output")
            if isinstance(output, list):
                images = [
                    part for part in output if isinstance(part, dict) and part.get("type") == "input_image"
                ]
                if images:
                    remaining = [part for part in output if part not in images]
                    projected.append(
                        {
                            **item,
                            "output": remaining or "Image returned by tool; see the following image message.",
                        }
                    )
                    image_messages.append(
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": f"Image from tool call {call_id}:"},
                                *images,
                            ],
                        }
                    )
                else:
                    projected.append(item)
            else:
                projected.append(item)
            if isinstance(call_id, str):
                pending_calls.discard(call_id)
            if not pending_calls:
                projected.extend(image_messages)
                image_messages.clear()
        else:
            projected.append(item)

    projected.extend(image_messages)
    return projected


class PlatformChatCompletionsModel(RouteReasoningMixin, OpenAIChatCompletionsModel):
    """Use the SDK request/stream/retry implementation with image projection."""

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        # Both SDK get_response and stream_response pass through this method.
        # Keep provider parameters, response conversion and retry semantics in
        # the pinned SDK instead of maintaining a fork of its HTTP client.
        return await super()._fetch_response(system_instructions, project_tool_images(input), *args, **kwargs)
