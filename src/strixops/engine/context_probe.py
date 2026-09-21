"""A bounded startup probe; accepted input is a lower bound, never a context maximum.

Prompts contain synthetic text only. Byte-based input estimates plus a framing
reserve bound the planned spend without loading a tokenizer over the network.
Only an explicit provider context-limit error can replace the initial capacity.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import httpx

from strixops.config.context import ContextSettings
from strixops.config.model_options import resolved_api_mode, validate_model_options
from strixops.config.provider import strip_provider_prefix
from strixops.config.settings import EngineSettings
from strixops.engine.model_capacity import (
    CapacityProbe,
    ModelCapacity,
    ProbeStatus,
    _models_url,
    _positive_limit,
)

OUTPUT_TOKENS = 64
MAX_RESPONSE_BYTES = 128 * 1024
_FRAMING_TOKENS = 256
_CONTEXT_CODES = {
    "context_length_exceeded", "context_window_exceeded", "context_limit_exceeded",
    "prompt_too_long", "input_too_long",
}
_LIMIT_PATTERN = re.compile(
    r"(?:maximum|max)\s+(?:context\s+(?:length|window)|input\s+length)"
    r"\s*(?:is|of|:|=)?\s*([0-9][0-9,]{0,11})\s+tokens\b", re.IGNORECASE,
)
_PROMPT_PATTERN = re.compile(
    r"prompt is too long:\s*([0-9][0-9,]{0,11})\s+tokens\s*>\s*"
    r"([0-9][0-9,]{0,11})\s*(?:maximum|max)\b", re.IGNORECASE,
)
_INPUT_PATTERN = re.compile(r"([0-9][0-9,]{0,11})\s+(?:tokens\s+)?in (?:the )?messages\b", re.IGNORECASE)


def _number(text: str) -> int | None:
    if not re.fullmatch(r"\d+|\d{1,3}(?:,\d{3})+", text):
        return None
    return _positive_limit(int(text.replace(",", "")))


def _context_error(payload: dict[str, Any]) -> tuple[bool, int | None, int | None]:
    error = payload.get("error")
    if not isinstance(error, dict):
        return False, None, None
    message = error.get("message")
    message = message if isinstance(message, str) else ""
    code = error.get("code")
    known_context = isinstance(code, str) and code.lower() in _CONTEXT_CODES
    prompt_match = _PROMPT_PATTERN.search(message)
    limit_match = _LIMIT_PATTERN.search(message)
    # A number next to an output-token parameter or a generic 400 is not a context limit.
    explicit_overflow = bool(prompt_match) or any(phrase in message.lower() for phrase in (
        "context length exceeded", "context window exceeded", "exceeds the context",
        "exceed the context", "however, you requested", "requested tokens exceed",
    ))
    if not known_context and not explicit_overflow:
        return False, None, None
    limits = [
        value for name in ("max_context_length", "context_length", "context_window", "max_input_tokens",
                           "max_model_len")
        if (value := _positive_limit(error.get(name))) is not None
    ]
    rejected = _positive_limit(error.get("input_tokens")) or _positive_limit(error.get("prompt_tokens"))
    if limit_match and (value := _number(limit_match.group(1))) is not None:
        limits.append(value)
    if prompt_match:
        rejected = _number(prompt_match.group(1))
        limit = _number(prompt_match.group(2))
        if limit is not None and rejected is not None and rejected > limit:
            limits.append(limit)
    if rejected is None and (match := _INPUT_PATTERN.search(message)):
        rejected = _number(match.group(1))
    return True, min(limits, default=None), rejected


def _prompt(target: int) -> tuple[str, str]:
    first, last = secrets.token_hex(6), secrets.token_hex(6)
    prefix = (
        f"First marker: {first}\nThis is synthetic context testing text. "
        "Reply only with the first and last markers, separated by one space.\n"
    )
    suffix = f"\nLast marker: {last}"
    filler_bytes = target - _FRAMING_TOKENS - len(prefix) - len(suffix)
    filler = secrets.token_hex((max(0, filler_bytes) + 1) // 2)[:max(0, filler_bytes)]
    return prefix + filler + suffix, f"{first} {last}"


def _openrouter_chat(settings: EngineSettings, mode: str) -> bool:
    parts = urlsplit(settings.llm_api_base)
    return (
        mode == "chat_completions" and parts.scheme == "https"
        and parts.hostname == "openrouter.ai" and parts.port in {None, 443}
    )


def _request_body(
    settings: EngineSettings, model: str, mode: str, prompt: str, output_tokens: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "stream": False, "store": False}
    if mode == "responses":
        body.update(input=[{"role": "user", "content": prompt}], max_output_tokens=output_tokens,
                    truncation="disabled")
        if settings.llm_reasoning_effort != "default":
            body["reasoning"] = {"effort": settings.llm_reasoning_effort}
    else:
        body.update(messages=[{"role": "user", "content": prompt}], max_tokens=output_tokens)
        if settings.llm_reasoning_effort != "default":
            body["reasoning_effort"] = settings.llm_reasoning_effort
        if _openrouter_chat(settings, mode):
            # https://openrouter.ai/docs/guides/features/message-transforms
            body["plugins"] = [{"id": "context-compression", "enabled": False}]
    return body


def _report_usage(
    payload: dict[str, Any], mode: str, callback: Callable[[dict[str, int]], None] | None,
) -> None:
    usage = payload.get("usage")
    if callback is None or not isinstance(usage, dict):
        return
    input_key, output_key = (
        ("input_tokens", "output_tokens") if mode == "responses" else ("prompt_tokens", "completion_tokens")
    )
    values = [usage.get(key) for key in (input_key, output_key, "total_tokens")]
    if not all(type(value) is int and 0 <= value <= 100_000_000 for value in values):
        return
    inputs, outputs, total = values
    if inputs <= 0 or total < inputs + outputs:
        return
    # An accounting sink must not break startup probing.
    with suppress(Exception):
        callback({"input_tokens": inputs, "output_tokens": outputs, "total_tokens": total})


def _accepted_tokens(payload: dict[str, Any], mode: str, receipt: str, planned: int) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict) or payload.get("truncated") or payload.get("input_truncated"):
        return None
    if payload.get("truncation") is not None and payload["truncation"] != "disabled":
        return None
    count = _positive_limit(usage.get("input_tokens" if mode == "responses" else "prompt_tokens"))
    if count is None or count > planned:
        return None
    if mode == "responses":
        if payload.get("status") != "completed" or not isinstance(payload.get("output"), list):
            return None
        parts = []
        for item in payload["output"]:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                return None
            parts.extend(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "output_text"
            )
        if not all(isinstance(part, str) for part in parts):
            return None
        text = "".join(parts)
    else:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return None
        choice = choices[0]
        message = choice.get("message")
        if choice.get("finish_reason") != "stop" or not isinstance(message, dict):
            return None
        text = message.get("content")
    return count if isinstance(text, str) and " ".join(text.split()) == receipt else None


async def _request(
    client: httpx.AsyncClient, url: str, key: str, body: dict[str, Any], timeout: float,
) -> tuple[int, dict[str, Any] | None]:
    async with asyncio.timeout(timeout):
        async with client.stream(
            "POST", url, json=body, auth=None, follow_redirects=False, timeout=timeout,
            headers={
                "Authorization": f"Bearer {key}", "Accept": "application/json", "Accept-Encoding": "identity",
            },
        ) as response:
            status = response.status_code
            if status not in {200, 400, 413, 422}:
                return status, None
            if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                return status, None
            size = response.headers.get("content-length", "")
            if size.isascii() and size.isdigit() and (len(size) > 10 or int(size) > MAX_RESPONSE_BYTES):
                return status, None
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                    return status, None
                data.extend(chunk)
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            return status, None
        return status, payload if isinstance(payload, dict) else None


async def probe_model_capacity(
    settings: EngineSettings, context: ContextSettings, capacity: ModelCapacity,
    *, on_usage: Callable[[dict[str, int]], None] | None = None,
) -> ModelCapacity:
    """Probe a startup snapshot once; errors and prompts never leave this function."""
    if capacity.probe is not None:
        return capacity
    if capacity.capacity_source not in {"model_catalog", "configured_fallback"}:
        return replace(capacity, probe=CapacityProbe(status="skipped_metadata"))
    if not context.probe_enabled:
        return replace(capacity, probe=CapacityProbe(status="disabled"))
    model = strip_provider_prefix(settings.strix_llm)
    models_url = _models_url(settings.llm_api_base)
    if (
        model != capacity.model or models_url is None or not settings.llm_api_key
        or validate_model_options(model, settings.llm_api_mode, settings.llm_reasoning_effort)
    ):
        return replace(capacity, probe=CapacityProbe(status="failed"))
    mode = resolved_api_mode(model, settings.llm_api_mode)
    url = models_url.removesuffix("/models") + ("/responses" if mode == "responses" else "/chat/completions")
    requests = spent = accepted_target = 0
    accepted = rejected = rejected_target = None
    status: ProbeStatus = "budget_exhausted"
    reported_limit = None
    target = min(8_192, context.probe_max_input_tokens, context.probe_total_input_tokens)
    output_budget = min(OUTPUT_TOKENS, capacity.output_limit_tokens)
    try:
        async with asyncio.timeout(context.probe_timeout_seconds):
            # Direct HTTP has no SDK retry layer, and this client is never shared with scan calls.
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                while (
                    requests < context.probe_max_requests
                    and target + spent <= context.probe_total_input_tokens
                ):
                    prompt, receipt = _prompt(target)
                    body = _request_body(settings, model, mode, prompt, output_budget)
                    requests += 1
                    spent += target
                    http_status, payload = await _request(
                        client, url, settings.llm_api_key, body, context.probe_request_timeout_seconds,
                    )
                    if payload is None:
                        status = "failed"
                        break
                    if http_status == 200:
                        _report_usage(payload, mode, on_usage)
                        count = _accepted_tokens(payload, mode, receipt, target)
                        # Generic chat gateways have no standard no-truncation switch.
                        # Reading both edges alone cannot detect middle-out compression.
                        if count is None or (mode != "responses" and not _openrouter_chat(settings, mode)):
                            status = "unverified"
                            break
                        accepted = max(accepted or 0, count)
                        accepted_target = target
                        if target >= context.probe_max_input_tokens:
                            status = "completed"
                            break
                    else:
                        is_context, limit, count = _context_error(payload)
                        if not is_context:
                            status = "failed"
                            break
                        if count is not None:
                            rejected = min(rejected or count, count)
                        if limit is not None:
                            if (
                                limit < (accepted or 0) or limit > target + output_budget
                                or (count is not None and count + output_budget <= limit)
                            ):
                                status = "unverified"
                                break
                            reported_limit = limit
                            status = "limit_reported"
                            break
                        if http_status == 413:
                            status = "failed"
                            break
                        rejected_target = target
                    next_target = (
                        (accepted_target + rejected_target) // 2 if rejected_target is not None
                        else min(target * 2, context.probe_max_input_tokens)
                    )
                    if next_target < 512 or next_target == target or next_target <= accepted_target:
                        break
                    target = next_target
    except (TimeoutError, httpx.TimeoutException):
        status = "timeout"
    except (httpx.HTTPError, httpx.InvalidURL, OSError, UnicodeError, ValueError):
        status = "failed"
    probe = CapacityProbe(
        status=status, requests=requests, largest_accepted_input_tokens=accepted,
        smallest_rejected_input_tokens=rejected, output_budget_tokens=output_budget,
        planned_input_tokens=spent,
    )
    if reported_limit is not None:
        return replace(
            capacity, capacity_tokens=reported_limit, capacity_source="provider_error", probe=probe,
        )
    return replace(capacity, probe=probe)
