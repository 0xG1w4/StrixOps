"""Resolve one run's model limits without inference calls or credential-bearing diagnostics.

Provider metadata is scoped to this call, never cached by model name. The fallback
catalog is read directly from the installed LiteLLM distribution; importing its
metadata loader would permit unrelated network requests during startup.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, distribution
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from strixops.config.context import ContextSettings
from strixops.config.provider import strip_provider_prefix
from strixops.config.settings import EngineSettings

REQUEST_TIMEOUT_SECONDS = 6.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_CATALOG_BYTES = 8 * 1024 * 1024
_MAX_TOKEN_LIMIT = 100_000_000
_DEFAULT_OUTPUT_TOKENS = 8_192
_CAPACITY_FIELDS = ("context_length", "context_window", "max_input_tokens", "max_model_len")
_OUTPUT_FIELDS = ("max_output_tokens", "max_completion_tokens")

CapacitySource = Literal["provider_metadata", "provider_error", "model_catalog", "configured_fallback"]
LookupStatus = Literal[
    "resolved", "no_metadata", "model_not_found", "invalid_endpoint", "redirect",
    "auth_error", "http_error", "invalid_response", "response_too_large", "timeout",
    "connection_error",
]


ProbeStatus = Literal[
    "skipped_metadata", "disabled", "completed", "limit_reported", "budget_exhausted",
    "timeout", "failed", "unverified",
]


@dataclass(frozen=True)
class CapacityProbe:
    status: ProbeStatus
    requests: int = 0
    largest_accepted_input_tokens: int | None = None
    smallest_rejected_input_tokens: int | None = None
    output_budget_tokens: int = 64
    planned_input_tokens: int = 0


@dataclass(frozen=True)
class ModelCapacity:
    model: str
    capacity_tokens: int
    output_limit_tokens: int
    capacity_source: CapacitySource
    output_source: CapacitySource
    lookup_status: LookupStatus
    probe: CapacityProbe | None = None

    def to_dict(self) -> dict[str, Any]:
        """Only model identity, limits and fixed provenance labels are persisted."""
        result = asdict(self)
        if self.probe is None:
            result.pop("probe")
        return result


def _positive_limit(value: Any) -> int | None:
    # bool is an int subclass; floats and numeric strings are not published integers.
    if type(value) is int and 0 < value <= _MAX_TOKEN_LIMIT:
        return value
    return None


def _minimum(values: list[int | None]) -> int | None:
    return min((value for value in values if value is not None), default=None)


def _limits(item: dict[str, Any]) -> tuple[int | None, int | None]:
    capacities = [_positive_limit(item.get(field)) for field in _CAPACITY_FIELDS]
    outputs = [_positive_limit(item.get(field)) for field in _OUTPUT_FIELDS]
    top_provider = item.get("top_provider")
    if isinstance(top_provider, dict):
        capacities.append(_positive_limit(top_provider.get("context_length")))
        outputs.extend(_positive_limit(top_provider.get(field)) for field in _OUTPUT_FIELDS)
    # max_tokens is deliberately absent: providers often use it for output tokens.
    return _minimum(capacities), _minimum(outputs)


def _models_url(base: str) -> str | None:
    base = base.strip()
    if not base or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in base):
        return None
    if any(char in base for char in ("\\", "?", "#")):
        return None
    try:
        parts = urlsplit(base)
        if (
            parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
        ):
            return None
        port = parts.port
        host = parts.hostname.encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError):
        return None
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        authority += f":{port}"
    return urlunsplit((parts.scheme, authority, parts.path.rstrip("/") + "/models", "", ""))


async def _request_limits(
    client: httpx.AsyncClient, url: str, key: str, model: str,
) -> tuple[int | None, int | None, LookupStatus]:
    async with client.stream(
        "GET", url,
        headers={
            "Authorization": f"Bearer {key}", "Accept": "application/json", "Accept-Encoding": "identity",
        },
        auth=None, follow_redirects=False, timeout=REQUEST_TIMEOUT_SECONDS,
    ) as response:
        if 300 <= response.status_code < 400:
            return None, None, "redirect"
        if response.status_code in {401, 403}:
            return None, None, "auth_error"
        if not 200 <= response.status_code < 300:
            return None, None, "http_error"
        # Prevent decompression from allocating an unbounded body before our size check.
        if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
            return None, None, "invalid_response"
        declared_size = response.headers.get("content-length", "")
        if (
            declared_size.isascii() and declared_size.isdigit()
            and (len(declared_size) > 10 or int(declared_size) > MAX_RESPONSE_BYTES)
        ):
            return None, None, "response_too_large"
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                return None, None, "response_too_large"
            body.extend(chunk)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return None, None, "invalid_response"
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None, None, "invalid_response"
    matches = [item for item in payload["data"] if isinstance(item, dict) and item.get("id") == model]
    if not matches:
        return None, None, "model_not_found"
    limits = [_limits(item) for item in matches]
    capacity = _minimum([value[0] for value in limits])
    output = _minimum([value[1] for value in limits])
    return capacity, output, "resolved" if capacity is not None else "no_metadata"


async def _provider_limits(
    settings: EngineSettings, model: str, client: httpx.AsyncClient | None,
) -> tuple[int | None, int | None, LookupStatus]:
    url = _models_url(settings.llm_api_base)
    if url is None:
        return None, None, "invalid_endpoint"
    try:
        # Includes connection, headers and the entire streamed body, not just read inactivity.
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            if client is not None:
                return await _request_limits(client, url, settings.llm_api_key, model)
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as owned_client:
                return await _request_limits(owned_client, url, settings.llm_api_key, model)
    except (TimeoutError, httpx.TimeoutException):
        return None, None, "timeout"
    except (httpx.HTTPError, httpx.InvalidURL, OSError, UnicodeError, ValueError):
        return None, None, "connection_error"


def _catalog_limits(model: str) -> tuple[int | None, int | None]:
    try:
        path = distribution("litellm").locate_file("litellm/model_prices_and_context_window_backup.json")
        with path.open("rb") as catalog:
            body = catalog.read(_MAX_CATALOG_BYTES + 1)
        if len(body) > _MAX_CATALOG_BYTES:
            return None, None
        payload = json.loads(body)
    except (PackageNotFoundError, OSError, ValueError, UnicodeError, RecursionError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    # The bundled table sometimes adds its routing wrapper to a vendor/model slug.
    # Do not guess aliases by removing vendor names or matching model-name substrings.
    for key in (model, f"openrouter/{model}"):
        item = payload.get(key)
        if isinstance(item, dict):
            limits = _limits(item)
            if any(value is not None for value in limits):
                return limits
    return None, None


async def resolve_model_capacity(
    settings: EngineSettings,
    context: ContextSettings,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> ModelCapacity:
    """Resolve the effective route once; caller-owned clients remain open.

    ``http_client`` is an injection point for trusted transports (including tests).
    Production-owned clients disable ambient proxies and credentials. Neither raw
    provider responses nor exceptions are exposed through the returned snapshot.
    """
    model = strip_provider_prefix(settings.strix_llm)
    capacity, output, status = await _provider_limits(settings, model, http_client)
    local_capacity, local_output = (
        await asyncio.to_thread(_catalog_limits, model)
        if capacity is None or output is None else (None, None)
    )
    return ModelCapacity(
        model=model,
        capacity_tokens=capacity or local_capacity or context.fallback_context_tokens,
        output_limit_tokens=output or local_output or _DEFAULT_OUTPUT_TOKENS,
        capacity_source=(
            "provider_metadata" if capacity is not None
            else "model_catalog" if local_capacity is not None else "configured_fallback"
        ),
        output_source=(
            "provider_metadata" if output is not None
            else "model_catalog" if local_output is not None else "configured_fallback"
        ),
        lookup_status=status,
    )
