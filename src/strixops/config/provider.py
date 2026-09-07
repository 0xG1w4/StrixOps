"""Platform LLM provider — every platform route is OpenAI-compatible.

The platform's runtime profiles produce exactly two route types (custom
OpenAI-compatible proxy, or OpenRouter) and always pass ``LLM_API_BASE`` +
``LLM_API_KEY`` + ``STRIX_LLM``. LiteLLM provider prefixes on the model name
(``openrouter/…``, ``openai/…``) are meaningful to LiteLLM routing but not to
a direct OpenAI-compatible call — the platform's own report workers strip
them the same way (``apps/api/services/direct_report.py``). Exotic providers
(Bedrock/Vertex) are outside the platform subset and unsupported.
"""

from __future__ import annotations

import httpx
from agents.models.interface import Model
from openai import AsyncOpenAI

from strixops.config.model_options import resolved_api_mode, validate_model_options
from strixops.config.responses_transport import PlatformResponsesModel
from strixops.config.settings import EngineSettings
from strixops.config.vision_transport import PlatformChatCompletionsModel

# Outer routing prefixes a caller may wrap around the model name. Vendor
# prefixes that are PART of a catalog slug (OpenRouter "openai/gpt-5",
# "anthropic/claude-…") must survive — only the redundant outer wrapper goes.
_STRIPPABLE_PREFIXES = ("openrouter/", "litellm/")


def strip_provider_prefix(model: str) -> str:
    name = (model or "").strip()
    changed = True
    while changed:
        changed = False
        for prefix in _STRIPPABLE_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                changed = True
    return name


def make_platform_model(
    settings: EngineSettings,
    model_override: str | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> Model:
    model_name = strip_provider_prefix(model_override or settings.strix_llm)
    problems = validate_model_options(model_name, settings.llm_api_mode, settings.llm_reasoning_effort)
    if problems:
        raise ValueError("Invalid model route: " + "; ".join(problems))
    client = AsyncOpenAI(
        base_url=settings.llm_api_base or None,
        api_key=settings.llm_api_key or "missing",
        **({"http_client": http_client} if http_client is not None else {}),
    )
    model_class = (
        PlatformResponsesModel
        if resolved_api_mode(model_name, settings.llm_api_mode) == "responses"
        else PlatformChatCompletionsModel
    )
    return model_class(model=model_name, openai_client=client, reasoning_effort=settings.llm_reasoning_effort)
