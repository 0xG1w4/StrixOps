"""Provider-agnostic conversation compaction.

Adapted from Strix context management for the existing platform model route.
When an agent's session grows past the model's usable context window, older
turns are summarised into a single checkpoint while the most recent turns are
kept verbatim. Summaries use the task's configured OpenAI-compatible model,
keep the reference's security-focused structure, and preserve tool-call/result
pairing so the trimmed history is still valid provider input.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

import httpx
from agents.exceptions import ModelBehaviorError
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from openai import APIConnectionError, APIStatusError, APITimeoutError
from openai import BadRequestError as OpenAIBadRequestError
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from strixops.config.context import ContextSettings
from strixops.config.model_errors import format_model_error, upstream_policy_code
from strixops.engine.context_budget import (
    context_limit_from_error,
    context_window,
    count_tokens,
    output_limit,
)
from strixops.engine.resilience import MODEL_RETRY
from strixops.engine.sessions import replace_session_items, session_write_lock

if TYPE_CHECKING:
    from agents.items import ModelResponse
    from agents.memory import Session


logger = logging.getLogger(__name__)

_CHECKPOINT_TAG = "<conversation-checkpoint>"
_TOOL_OUTPUT_MAX_CHARS = 2_000
_IMAGE_TOKEN_ESTIMATE = 4_096
_HEAD_TRUNCATED_MARKER = "\n\n[... older conversation omitted to fit the summary request ...]\n\n"
_SUMMARY_TOTAL_TIMEOUT = 180.0
_SUMMARY_ATTEMPT_TIMEOUT = 60.0
_SUMMARY_RETRY_TIMEOUT = 120.0
_SUMMARY_RETRY_OUTPUT_LIMIT = 8_192
_CONCISE_SUMMARY_RETRY = (
    "\n\nThis is a bounded retry. Return a shorter, complete checkpoint using the required sections. "
    "Prioritize distinct verified facts, exact continuation identifiers, scope, active work and cleanup. "
    "Use concise bullets and saved-file references; omit repeated explanation and do not investigate anew. "
    "Finish every section within the available output budget.\n"
)


@dataclass
class CompactionDiagnostics:
    """Bounded outcome details; token counts are full input estimates before/after compaction."""

    status: str = "not_attempted"
    detail: str = ""
    retryable: bool = False
    summary_attempts: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None


class _SummaryFailure(Exception):
    def __init__(self, status: str, detail: str, *, retryable: bool) -> None:
        super().__init__(detail)
        self.status = status
        self.retryable = retryable


class ContextBudgetExceeded(Exception):
    """Local preflight stopped a request before it reached the provider."""

    def __init__(self, used: int, budget: int) -> None:
        self.used_tokens = used
        self.budget_tokens = budget
        super().__init__(f"Estimated context input {used} tokens exceeds the safe input budget {budget}")


def _request_item_tokens(model: str, item: Any) -> int:
    # Budget full text/arguments/results, not the summary previews below. Image
    # data URLs are decoded by vision providers, not tokenized as base64 text;
    # counting those bytes would discard fresh screenshots before the model saw
    # them. Use an image allowance alongside the existing image-count limit.
    image_tokens = 0

    def text_payload(value: Any) -> Any:
        nonlocal image_tokens
        if isinstance(value, dict):
            if value.get("type") in {"input_image", "image_url", "output_image"}:
                image_tokens += _IMAGE_TOKEN_ESTIMATE
                return {"type": value["type"], "image": "[image]"}
            return {key: text_payload(nested) for key, nested in value.items()}
        if isinstance(value, list):
            return [text_payload(nested) for nested in value]
        return value

    serialized = json.dumps(text_payload(item), ensure_ascii=False, default=str)
    return count_tokens(model, serialized) + image_tokens


def estimate_input_tokens(
    model: str, items: list[Any], instructions: str = "", tools_text: str = ""
) -> int:
    return count_tokens(model, "\n".join((instructions, tools_text))) + sum(
        _request_item_tokens(model, item) for item in items
    )


def _effective_window(model: str, settings: ContextSettings, context_window_limit: int | None) -> int:
    window = context_window(model, settings)
    return min(window, context_window_limit) if context_window_limit is not None else window


def input_budget(
    model: str, settings: ContextSettings, context_window_limit: int | None = None
) -> int:
    window = _effective_window(model, settings, context_window_limit)
    # Output metadata is a maximum capability, not the request's max_tokens.
    # An alias may advertise more output capacity than the routed model's entire
    # window. Keep usable input space instead of getting stuck at a zero budget.
    reserve = min(max(settings.compact_buffer_tokens, output_limit(model)), max(1, window // 2))
    return max(0, window - reserve)


def retry_input_budget(
    model: str, settings: ContextSettings, context_window_limit: int | None = None
) -> int:
    """Conservative input allowance for one continuation after a soft-budget failure."""
    window = _effective_window(model, settings, context_window_limit)
    reserve = min(output_limit(model), max(1, window // 2)) + max(1024, window // 100)
    return max(0, window - reserve)


# Providers that don't type overflow errors (OpenRouter maps every 400 to a
# plain BadRequestError) leave only the message to go on, so we match it the way
# LiteLLM's own checker does — but with rate-limit exclusions first, so a
# throttling 429 is never mistaken for an overflow and sent into compaction.
_OVERFLOW_EXCLUSIONS = (
    "rate limit",
    "too many requests",
    "throttling",
    "service unavailable",
    "quota",
)
_OVERFLOW_MARKERS = (
    "context length",
    "context window",
    "context_length_exceeded",
    "prompt is too long",
    "input is too long",
    "input length",
    "maximum prompt length",
    "reduce the length of the messages",
    "too many tokens",
    "token limit exceeded",
    "request entity too large",
)


@cache
def _overflow_error_types() -> tuple[type[BaseException], type[BaseException]]:
    """``(ContextWindowExceededError, BadRequestError)``, imported on first use.

    LiteLLM costs seconds to import, and nothing needs it until a model call is
    actually made, so it stays off the launch path.
    """
    from litellm.exceptions import BadRequestError, ContextWindowExceededError

    return ContextWindowExceededError, BadRequestError


def is_context_overflow(exc: BaseException) -> bool:
    """Whether ``exc`` is a model context-window-overflow error.

    LiteLLM types most providers' overflow as ContextWindowExceededError, but its
    OpenRouter branch raises a plain BadRequestError, so for that we fall back to
    matching the provider message.
    """
    if isinstance(exc, ContextBudgetExceeded):
        return True
    context_window_exceeded, bad_request = _overflow_error_types()
    if isinstance(exc, context_window_exceeded):
        return True
    if isinstance(exc, (bad_request, OpenAIBadRequestError)):
        # Some gateways leave str(exc) at "Error code: 400" and put the
        # provider's context rejection exclusively in the nested error body.
        if context_limit_from_error(exc) is not None:
            return True
        msg = str(exc).lower()
        if any(x in msg for x in _OVERFLOW_EXCLUSIONS):
            return False
        return any(x in msg for x in _OVERFLOW_MARKERS)
    return False


_SUMMARY_INSTRUCTIONS = """\
You are compacting the earlier part of an autonomous security-testing agent's \
conversation so it fits the model context window. Produce a dense, factual \
record that lets the agent continue with no loss of important state.

Preserve every distinct finding, its validation status and evidence references. \
Keep the authorized scope, operator constraints, verified execution host/session, \
active work, resource ownership and unresolved cleanup explicit. Deduplicate \
repeated observations but never merge distinct findings or turn an observation \
into a verified result. Preserve exact identifiers needed to continue: URLs, \
paths, parameters, finding/resource IDs and relevant errors. When complete \
datasets already exist in saved evidence, retain their path, record count and \
validation status instead of copying every row into this summary. Do not invent \
evidence files or discard unique unsaved evidence; flag unsaved data for immediate \
preservation. Do not invent anything or describe this compaction process.

Return Markdown with exactly these sections:

## Objective
The overall goal and target scope.

## Vulnerabilities & Findings
One bullet per DISTINCT vulnerability or finding (SQLi, XSS, SSRF, auth bypass, \
misconfig, etc.). For each: type, exact location (URL/endpoint/param/file), the \
verbatim payload or proof, confirmation status, and impact. List them all.

## Credentials & Secrets
Record credential identities, their applicability and validation status, and \
references to complete saved evidence. Preserve an exact value only when it is \
needed for the next action or is not recoverable from an existing saved file. \
Do not expand attached datasets into per-row lists. Write "(none)" only if truly none.

## System & Recon Details
Architecture, tech stack, versions, discovered endpoints/paths/params, and \
other weak points worth keeping.

## Work State
- Completed: what has been verified or finished.
- Active: what is in progress right now.
- Blocked: anything stuck and why.
- Cleanup: open resource IDs, original-state references, verified removals and \
explicit retention authorization. For internal scans, read get_internal_campaign \
for the latest inventory.

## Failed Attempts & Dead Ends
One bullet per approach already tried that did not work (including WAF blocks, \
filtered inputs, non-exploitable leads) so they are not repeated. Write \
"(none)" only if truly none.

## Next Move
The concrete next step(s) the agent intended to take.

## Relevant Files
Files/notes/reports created or modified and their purpose."""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
            elif block.get("type") in {"input_image", "image_url", "output_image"}:
                parts.append("[image]")
        return "\n".join(parts)
    return ""


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n[truncated]"


def _serialize_item(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    item_type = item.get("type")
    role = item.get("role")
    if item_type == "function_call":
        args = _truncate(str(item.get("arguments", "")), _TOOL_OUTPUT_MAX_CHARS)
        return f"[tool_call {item.get('name', '?')}] {args}"
    if item_type == "function_call_output":
        output = item.get("output")
        text = output if isinstance(output, str) else _content_text(output)
        return f"[tool_result] {_truncate(text, _TOOL_OUTPUT_MAX_CHARS)}"
    if item_type == "reasoning":
        return ""
    if role or item_type == "message":
        return f"[{role or 'assistant'}] {_content_text(item.get('content'))}".strip()
    return ""


def _serialize_items(items: list[Any]) -> str:
    return "\n".join(s for s in (_serialize_item(item) for item in items) if s)


def _is_tool_call(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "function_call"


def _is_tool_output(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "function_call_output"


def _open_calls_at(items: list[Any]) -> list[int]:
    """Prefix count of tool calls still awaiting their result at each index;
    a split is only safe where this is zero."""
    balance = [0] * (len(items) + 1)
    for i, item in enumerate(items):
        delta = 1 if _is_tool_call(item) else -1 if _is_tool_output(item) else 0
        balance[i + 1] = max(0, balance[i] + delta)
    return balance


def _select_split(model: str, items: list[Any], keep_tokens: int) -> int:
    """Index where the kept-verbatim recent tail begins: walk newest→oldest to
    ``keep_tokens``, then advance to a point with no tool call left open.

    Moving backward would keep an entire oversized call/result group, defeating
    the budget even when only the final small result fitted in the tail.
    """
    total = 0
    split = len(items)
    for i in range(len(items) - 1, -1, -1):
        total += _request_item_tokens(model, items[i])
        if total > keep_tokens:
            break
        split = i
    open_calls = _open_calls_at(items)
    while split < len(items) and open_calls[split] != 0:
        split += 1
    return split


def _previous_summary(head: list[Any]) -> str | None:
    for item in head:
        if isinstance(item, dict) and item.get("role") == "user":
            text = _content_text(item.get("content"))
            if text.startswith(_CHECKPOINT_TAG):
                return text
    return None


def _fit_to_tokens(model: str, text: str, max_tokens: int) -> str:
    """Head+tail-truncate ``text`` to ``max_tokens``, keeping start and end."""
    if count_tokens(model, text) <= max_tokens:
        return text
    if count_tokens(model, _HEAD_TRUNCATED_MARKER) > max_tokens:
        return ""
    # Rough char budget (~4x tokens), then tighten by real token count.
    budget_chars = max_tokens * 4
    head_chars = budget_chars // 2
    tail_chars = budget_chars - head_chars
    candidate = text[:head_chars] + _HEAD_TRUNCATED_MARKER + text[len(text) - tail_chars :]
    while count_tokens(model, candidate) > max_tokens and (head_chars > 0 or tail_chars > 0):
        head_chars = int(head_chars * 0.8)
        tail_chars = int(tail_chars * 0.8)
        candidate = text[:head_chars] + _HEAD_TRUNCATED_MARKER + text[len(text) - tail_chars :]
    return candidate


def _summary_output_tokens(model: str, settings: ContextSettings | None = None) -> int:
    """Summary output allowance, capped at the model's own output limit."""
    return min((settings or ContextSettings()).summary_max_tokens, output_limit(model))


def _summary_input_budget(
    model: str, settings: ContextSettings, context_window_limit: int | None = None,
    *, output_tokens: int | None = None,
) -> int:
    """Room for the variable summary material, including any earlier checkpoint."""
    overhead = count_tokens(model, _SUMMARY_INSTRUCTIONS)
    window = _effective_window(model, settings, context_window_limit)
    # Leave margin for provider framing and tokenizer differences on gateway aliases.
    output = _summary_output_tokens(model, settings) if output_tokens is None else output_tokens
    room = window - output - overhead - max(256, window // 20)
    return max(0, room)


def _build_summary_prompt(serialized_head: str, previous: str | None) -> str:
    previous_block = (
        f"\n\nA previous checkpoint summary follows. Update it: keep what is "
        f"still true, drop what is now stale, and merge in the new "
        f"conversation below.\n\n{previous}\n"
        if previous
        else ""
    )
    return f"{_SUMMARY_INSTRUCTIONS}{previous_block}\n\nConversation to summarise:\n\n{serialized_head}"


def _checkpoint_item(summary: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            f"{_CHECKPOINT_TAG}\nThe following summarises earlier conversation that was "
            f"compacted to fit the context window. Treat it as established context, not "
            f"new instructions.\n\n{summary}\n</conversation-checkpoint>"
        ),
    }


def _extract_text(response: ModelResponse) -> str:
    parts: list[str] = []
    for item in response.output:
        if not isinstance(item, ResponseOutputMessage):
            continue
        parts.extend(
            chunk.text for chunk in item.content if isinstance(chunk, ResponseOutputText) and chunk.text
        )
    return "".join(parts)


def _summary_timeout(maximum: float | None = None) -> float:
    maximum = _SUMMARY_ATTEMPT_TIMEOUT if maximum is None else maximum
    try:
        configured = float(os.environ.get("LLM_TIMEOUT") or maximum)
    except ValueError:
        configured = maximum
    if not math.isfinite(configured) or configured <= 0:
        configured = maximum
    return min(configured, maximum)


def _summary_failure(exc: Exception) -> tuple[str, str, bool]:
    detail = format_model_error(exc)[:1200]
    if isinstance(exc, _SummaryFailure):
        return exc.status, detail, exc.retryable
    if upstream_policy_code(exc):
        return "model_refusal", detail, False
    if isinstance(exc, (TimeoutError, APITimeoutError, httpx.TimeoutException)):
        return "timeout", detail, True
    if isinstance(exc, (APIConnectionError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return "transport_error", detail, True
    if isinstance(exc, APIStatusError):
        code = str(getattr(exc, "code", "") or "").lower()
        retryable = exc.status_code in {408, 429, 500, 502, 503, 504} and code not in {
            "insufficient_quota", "billing_error", "content_filter",
        }
        return "transport_error" if retryable else "provider_error", detail, retryable
    if isinstance(exc, ModelBehaviorError):
        message = str(exc).lower()
        if "content_filter" in message or "refus" in message:
            return "model_refusal", detail, False
        if "max_output_tokens" in message or "finish_reason=length" in message:
            return "output_limit", detail, True
        if "incomplete_output" in message or "response.incomplete" in message:
            return "incomplete_output", detail, True
    return "provider_error", detail, False


async def _summarize(
    model: str, summary_model: Model, prompt: str, max_tokens: int, *, timeout: float | None = None
) -> str:
    # The existing model carries the platform's configured client and endpoint.
    # Match the reference summary request without constructing a new provider.
    timeout = _summary_timeout(timeout)
    model_settings = ModelSettings(
        retry=MODEL_RETRY,
        include_usage=True,
        max_tokens=max_tokens,
        extra_args={"timeout": timeout},
    )
    response = await asyncio.wait_for(
        summary_model.get_response(
            system_instructions=None,
            input=prompt,
            model_settings=model_settings,
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ),
        timeout=timeout,
    )
    if any(getattr(item, "status", None) == "incomplete" for item in response.output):
        raise _SummaryFailure("incomplete_output", "Summary output is incomplete.", retryable=True)
    if any(
        getattr(chunk, "type", None) == "refusal"
        for item in response.output for chunk in (getattr(item, "content", None) or [])
    ):
        raise _SummaryFailure("model_refusal", "Summary output was refused.", retryable=False)
    content = _extract_text(response).strip()
    if not content:
        raise _SummaryFailure("empty_content", "Summary returned no visible text.", retryable=True)
    return content


async def maybe_compact(
    session: Session,
    *,
    model: str,
    summary_model: Model,
    instructions: str = "",
    tools_text: str = "",
    force: bool = False,
    settings: ContextSettings | None = None,
    context_window_limit: int | None = None,
    diagnostics: CompactionDiagnostics | None = None,
) -> bool:
    """Compact ``session`` if it is near the model's context window.

    Returns ``True`` when the session was rewritten. ``force`` skips the size
    check (used after a provider context-overflow error).
    """
    outcome = diagnostics if diagnostics is not None else CompactionDiagnostics()
    outcome.status, outcome.detail, outcome.retryable = "not_attempted", "", False
    outcome.summary_attempts = 0
    outcome.input_tokens = outcome.output_tokens = None

    def finish(status: str, detail: str, *, retryable: bool = False) -> bool:
        outcome.status, outcome.detail, outcome.retryable = status, detail, retryable
        return status == "compacted"

    context = settings or ContextSettings()
    if not context.auto_compact and not force:
        return finish("disabled", "Automatic compaction is disabled.")

    async with session_write_lock(session):
        items = list(await session.get_items())
    if not items:
        return finish("empty_session", "There are no saved session items to summarize.")

    budget = input_budget(model, context, context_window_limit)
    used = estimate_input_tokens(model, items, instructions, tools_text)
    outcome.input_tokens = used
    if not force and used <= budget:
        return finish("not_needed", "Saved session is within the input budget.")
    if estimate_input_tokens(model, items) <= estimate_input_tokens(model, [_checkpoint_item("")]):
        return finish("too_small", "Saved history is smaller than an empty checkpoint wrapper.")

    fixed_tokens = count_tokens(model, "\n".join((instructions, tools_text)))
    keep_tokens = min(context.keep_tokens, max(0, budget - fixed_tokens - context.summary_max_tokens - 256))
    split = _select_split(model, items, keep_tokens)
    if force and split == 0:
        # A short conversation can still exceed the real routed model's limit.
        # It may have no tail small enough to retain verbatim.
        split = len(items)
    head, recent = items[:split], items[split:]
    previous = _previous_summary(head)
    summary_output = _summary_output_tokens(model, context)
    summary_window_limit = context_window_limit
    summary_budget = _summary_input_budget(model, context, summary_window_limit, output_tokens=summary_output)
    if not head or summary_budget <= 0:
        # Nothing to summarise, or no room for even the summary request itself.
        if head:
            logger.warning(
                "skipping compaction for %s: no room to summarise within its context window", model
            )
        return finish("no_summary_room", "There is no summarizable history or room for a summary request.")

    # The previous checkpoint is supplied once, and shares the same bounded
    # material budget as the older turns; an oversized checkpoint cannot bypass it.
    serialized_head = _serialize_items([
        item for item in head
        if not (previous and isinstance(item, dict) and _content_text(item.get("content")) == previous)
    ])
    material = _build_summary_prompt(serialized_head, previous)[len(_SUMMARY_INSTRUCTIONS):]
    deadline = asyncio.get_running_loop().time() + _SUMMARY_TOTAL_TIMEOUT
    concise = False
    for attempt in range(2):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return finish("timeout", "Summary attempts exhausted their total time budget.", retryable=True)
        retry_instruction = _CONCISE_SUMMARY_RETRY if concise else ""
        room = _summary_input_budget(model, context, summary_window_limit, output_tokens=summary_output)
        room -= count_tokens(model, retry_instruction)
        fitted = _fit_to_tokens(model, material, min(summary_budget, room))
        if not fitted:
            return finish("no_summary_room", "The provider limit cannot fit the bounded summary request.")
        outcome.summary_attempts += 1
        try:
            attempt_timeout = _SUMMARY_ATTEMPT_TIMEOUT if attempt == 0 else _SUMMARY_RETRY_TIMEOUT
            summary = await _summarize(
                model,
                summary_model,
                _SUMMARY_INSTRUCTIONS + retry_instruction + fitted,
                summary_output,
                timeout=_summary_timeout(min(attempt_timeout, remaining)),
            )
        except Exception as exc:
            overflow = is_context_overflow(exc)
            status, detail, retryable = (
                ("context_overflow", format_model_error(exc)[:1200], True)
                if overflow else _summary_failure(exc)
            )
            logger.warning(
                "compaction summary attempt %d failed for %s: %s; %s", attempt + 1, model, status, detail
            )
            if attempt or not retryable:
                return finish(status, detail, retryable=retryable)
            if overflow:
                limit = context_limit_from_error(exc)
                if limit is not None:
                    summary_window_limit = min(summary_window_limit or limit, limit)
                summary_budget = min(
                    count_tokens(model, fitted) // 2,
                    _summary_input_budget(model, context, summary_window_limit, output_tokens=summary_output),
                )
            elif status in {"output_limit", "empty_content", "incomplete_output"}:
                summary_output = max(
                    summary_output, min(summary_output * 2, output_limit(model), _SUMMARY_RETRY_OUTPUT_LIMIT)
                )
                concise = True
            continue
        break

    new_items = [_checkpoint_item(summary), *recent]
    outcome.output_tokens = estimate_input_tokens(model, new_items, instructions, tools_text)
    if outcome.output_tokens >= used:
        logger.warning("compaction for %s did not reduce context size; keeping the original session", model)
        return finish(
            "not_smaller", "The complete checkpoint and retained tail are not smaller.", retryable=True
        )
    try:
        rewritten = await replace_session_items(session, new_items, expected_len=len(items))
    except Exception as exc:
        finish("session_write_error", format_model_error(exc)[:1200])
        raise
    if rewritten:
        logger.info(
            "compacted %s: %d items (~%d tok) -> %d items (summary + %d recent)",
            model,
            len(items),
            used,
            len(new_items),
            len(recent),
        )
        return finish("compacted", "Saved session was replaced with a smaller complete checkpoint.")
    return finish("session_changed", "Saved session changed during summarization; reload it.", retryable=True)
