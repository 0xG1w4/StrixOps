"""Model-aware token budgets, resolved from LiteLLM model metadata with a
large configurable fallback for models LiteLLM doesn't map.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from strixops.config.context import ContextSettings

logger = logging.getLogger(__name__)

# LiteLLM keys models without the routing prefix users type (``openai/``,
# ``litellm/``, ``ollama/`` ...). Strip a leading provider segment on lookup.
_STRIPPABLE_PREFIXES = (
    "openai/",
    "chatgpt/",
    "litellm/",
    "any-llm/",
    "ollama/",
    "ollama_chat/",
)

_DEFAULT_OUTPUT_TOKENS = 8_192
_CAPACITY_NUMBER = r"(?P<limit>(?:[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d{0,8}))(?![\d,.])"
_CONTEXT_CAPACITY_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:maximum|max)\s+(?:supported\s+)?(?:context\s+(?:length|window(?:\s+size)?|size)"
    r"|(?:input|prompt)\s+(?:length|tokens?))\s*(?:(?:is|of)\s*)?[:=(]?\s*"
    + _CAPACITY_NUMBER + r"(?:\s*tokens?\b|[.)]|\s*$)",
    r"\bcontext\s+(?:window(?:\s+(?:size|limit))?|length\s+limit)\s*(?:is|of|:|=)\s*"
    + _CAPACITY_NUMBER + r"(?:\s*tokens?\b|[.)]|\s*$)",
    r"\b(?:prompt|input)\s+is\s+too\s+long\s*:\s*\d[\d,]*\s+tokens?\s*>\s*"
    + _CAPACITY_NUMBER + r"\s+(?:tokens?\s+)?maximum\b",
))


def context_limit_from_error(exc: BaseException) -> int | None:
    """Read an explicit provider capacity, never a requested input/output count.

    This is a local parser, not model metadata. Callers may use its result to
    lower the current run's budget; it must not raise it or update shared caches.
    Only a 400 response or an explicitly typed overflow supplies a usable limit.
    """
    body = getattr(exc, "body", None)
    envelopes = [body] if isinstance(body, Mapping) else []
    if envelopes and isinstance(body.get("error"), Mapping):
        envelopes.append(body["error"])
    statuses = [getattr(exc, "status_code", None)]
    statuses.extend(value.get(key) for value in envelopes for key in ("status_code", "status"))
    statuses = [str(value) for value in statuses if re.fullmatch(r"[1-5]\d\d", str(value))]
    if any(value != "400" for value in statuses):
        return None
    labels = {
        str(value.get(key, "")).lower() for value in envelopes for key in ("code", "type")
    }
    if labels & {"insufficient_quota", "rate_limit_exceeded", "billing_error"}:
        return None
    typed_overflow = type(exc).__name__ == "ContextWindowExceededError" or bool(labels & {
        "context_length_exceeded", "context_window_exceeded", "contextwindowexceedederror",
    })
    if not statuses and not typed_overflow:
        return None
    messages = [value["message"] for value in envelopes if isinstance(value.get("message"), str)]
    messages.append(str(exc))
    for message in messages:
        message = message[:65536]
        if any(marker in message.lower() for marker in (
            "rate limit", "too many requests", "throttling", "service unavailable", "quota",
            "invalid schema", "invalid tool", "tool schema",
        )):
            return None
        capacities = [
            int(match["limit"].replace(",", ""))
            for pattern in _CONTEXT_CAPACITY_PATTERNS for match in pattern.finditer(message)
        ]
        if capacities:
            return min(capacities)
    return None


def _lookup_key(model: str) -> str:
    for prefix in _STRIPPABLE_PREFIXES:
        if model.startswith(prefix):
            return model[len(prefix) :]
    return model


def _safe_get_model_info(model: str) -> dict[str, Any] | None:
    try:
        import litellm

        return dict(litellm.get_model_info(model))
    except Exception:  # noqa: BLE001 - unmapped models raise; caller falls back.
        return None


@lru_cache(maxsize=128)
def _model_info(model: str) -> dict[str, int]:
    lookup_key = _lookup_key(model)
    # Provider-qualified ChatGPT lookups may start a synchronous device-login
    # poll. LiteLLM keys the metadata by the underlying model slug.
    candidates = (lookup_key,) if model.startswith("chatgpt/") else (model, lookup_key)
    for candidate in candidates:
        info = _safe_get_model_info(candidate)
        if info is not None:
            return {
                "max_input_tokens": int(info.get("max_input_tokens") or info.get("max_tokens") or 0),
                "max_output_tokens": int(info.get("max_output_tokens") or 0),
            }
    logger.debug("No LiteLLM model info for %r; using configured fallbacks", model)
    return {"max_input_tokens": 0, "max_output_tokens": 0}


def context_window(model: str, settings: ContextSettings | None = None) -> int:
    """Input token capacity for ``model`` (configured fallback when unmapped)."""
    resolved = _model_info(model)["max_input_tokens"]
    return resolved or (settings or ContextSettings()).fallback_context_tokens


def output_limit(model: str) -> int:
    """Max output tokens for ``model`` (a conservative default when unmapped)."""
    return _model_info(model)["max_output_tokens"] or _DEFAULT_OUTPUT_TOKENS


def count_tokens(model: str, text: str) -> int:
    """Token count for ``text`` under ``model``.

    Falls back to UTF-8 byte length (a guaranteed upper bound) when LiteLLM
    can't count, so budget checks stay conservative.
    """
    if not text:
        return 0
    try:
        import litellm

        return int(litellm.token_counter(model=_lookup_key(model), text=text))
    except Exception:  # noqa: BLE001 - tokenizer may be unavailable for some models.
        return len(text.encode("utf-8"))
