"""Shared, deterministic validation for a model route's API and reasoning options.

Documented model names guide Auto selection and reasoning-effort validation.
Explicit API choices remain configurable because gateways may translate them;
compatibility must be checked using the console's tool-call probe.
"""

from __future__ import annotations

import re

API_MODES = ("auto", "chat_completions", "responses")
REASONING_EFFORTS = ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max")

_ASTRA = re.compile(r"gpt-6-astra(?:-\d{4}-\d{2}-\d{2})?")
_GPT54_PRO = re.compile(r"gpt-5\.4-pro(?:-\d{4}-\d{2}-\d{2})?")
_GPT54_STANDARD = re.compile(r"gpt-5\.4(?:-mini|-nano)?(?:-\d{4}-\d{2}-\d{2})?")
_GPT55 = re.compile(r"gpt-5\.5(?:-\d{4}-\d{2}-\d{2})?")
_GPT56 = re.compile(r"gpt-5\.6(?:-sol|-terra|-luna)?(?:-\d{4}-\d{2}-\d{2})?")


def _known_model_name(model: str) -> str:
    name = model.strip().lower()
    while name.startswith(("openrouter/", "litellm/")):
        name = name.split("/", 1)[1]
    return name.removeprefix("openai/")


def resolved_api_mode(model: str, api_mode: str) -> str:
    """Resolve auto without probing endpoints or changing a failed request."""
    if api_mode not in API_MODES:
        raise ValueError(f"API mode must be one of: {', '.join(API_MODES)}")
    if api_mode == "auto":
        name = _known_model_name(model)
        needs_responses = any(pattern.fullmatch(name) for pattern in (_ASTRA, _GPT54_PRO, _GPT55, _GPT56))
        return "responses" if needs_responses else "chat_completions"
    return api_mode


def validate_model_options(model: str, api_mode: str, reasoning_effort: str) -> list[str]:
    """Validate route options for StrixOps agents, which always use function tools."""
    errors = []
    if api_mode not in API_MODES:
        errors.append(f"API mode must be one of: {', '.join(API_MODES)}")
    if reasoning_effort not in REASONING_EFFORTS:
        errors.append(f"Reasoning effort must be one of: {', '.join(REASONING_EFFORTS)}")
    if errors:
        return errors

    name = _known_model_name(model)
    # Model-specific subsets and provider defaults are from the official pages:
    # https://developers.openai.com/api/docs/models/gpt-6-astra
    # https://developers.openai.com/api/docs/models/gpt-5.4[-pro|-mini|-nano]
    # https://developers.openai.com/api/docs/models/gpt-5.5
    # https://developers.openai.com/api/docs/models/gpt-5.6-{sol,terra,luna}
    supported = None
    if _ASTRA.fullmatch(name):
        supported = ("low", "medium", "high", "xhigh", "max")
    elif _GPT54_PRO.fullmatch(name):
        supported = ("medium", "high", "xhigh")
    elif _GPT54_STANDARD.fullmatch(name) or _GPT55.fullmatch(name):
        supported = ("none", "low", "medium", "high", "xhigh")
    elif _GPT56.fullmatch(name):
        supported = ("none", "low", "medium", "high", "xhigh", "max")
    if supported and reasoning_effort != "default" and reasoning_effort not in supported:
        errors.append(
            f"{model} supports reasoning effort {', '.join(supported)}; choose one of these or default."
        )

    return errors
