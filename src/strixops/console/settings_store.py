"""Server-side model-profile store for the console.

Mirrors the platform's runtime-profile semantics, single-user: any number of
named profiles, exactly one active, each carrying an OpenAI-compatible route
(custom base or OpenRouter) plus per-scan-type agent models (web / internal).
The launcher resolves the active (or explicitly chosen) profile into the
engine's spawn env (``LLM_API_BASE`` / ``LLM_API_KEY`` / ``STRIX_LLM``) at
``POST /api/scans`` time.

Persistence: ``~/.strixops/console.json`` (0600). API keys never leave the
server in the clear — ``GET`` responses carry a masked form, and a masked
value written back means "unchanged".
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from strixops.config.model_options import validate_model_options
from strixops.console import json_store

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
SCHEMA_VERSION = 1
_ROUTE_TYPES = ("custom", "openrouter")
_KEY_MASK_PREFIX = "•••"

OPENROUTER_MODEL_PLACEHOLDER = "z-ai/glm-5.3"


def config_path() -> Path:
    override = (os.environ.get("STRIXOPS_CONSOLE_CONFIG") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".strixops" / "console.json"


def _normalize_settings(data: dict[str, Any]) -> dict[str, Any]:
    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        profiles = []
    active = data.get("active_profile_id")
    if not isinstance(active, str) or active not in {p.get("id") for p in profiles if isinstance(p, dict)}:
        active = None
    integrations = data.get("integrations")
    if not isinstance(integrations, dict):
        integrations = {}
    return {
        "schema_version": SCHEMA_VERSION,
        "active_profile_id": active,
        "profiles": [p for p in profiles if isinstance(p, dict)],
        "integrations": {
            "perplexity_api_key": integrations.get("perplexity_api_key") or "",
            "revision": json_store.revision(integrations.get("revision")),
            # Keep absence distinct from an explicit override: old files use
            # environment/default settings without a read-time migration write.
            **{
                name: integrations[name]
                for name in ("perplexity_enabled", "perplexity_model", "perplexity_timeout_seconds")
                if name in integrations
            },
        },
    }


def load_settings() -> dict[str, Any]:
    return _normalize_settings(json_store.read(config_path()))


@contextmanager
def settings_transaction() -> Iterator[dict[str, Any]]:
    """Edit normalized settings under one read/modify/write transaction."""
    with json_store.transaction(config_path()) as stored:
        settings = _normalize_settings(stored)
        yield settings
        stored.clear()
        stored.update(settings)


def save_settings(settings: dict[str, Any]) -> None:
    """Compatibility replacement; mutations should use settings_transaction."""
    with json_store.transaction(config_path()) as stored:
        stored.clear()
        stored.update(settings)


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    return f"{_KEY_MASK_PREFIX}{key[-4:]}" if len(key) > 4 else f"{_KEY_MASK_PREFIX}•"


def is_masked(value: str) -> bool:
    return value.startswith(_KEY_MASK_PREFIX)


def strip_provider_prefix(model: str) -> str:
    """Strip only the redundant OUTER wrapper (``openrouter/``); the vendor
    part of a catalog slug (``openai/gpt-5``) is the model name itself."""
    name = (model or "").strip()
    while name.startswith("openrouter/") or name.startswith("litellm/"):
        name = name.split("/", 1)[1].strip()
    return name


def _new_id() -> str:
    return f"p_{secrets.token_hex(4)}"


def _normalized_endpoint(base: str) -> tuple[Any, ...] | None:
    """Compare credential destinations, allowing host case and trailing slashes."""
    base = base.strip()
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in base):
        return None
    if any(char in base for char in ("\\", "?", "#")):
        return None
    try:
        parsed = urlsplit(base)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80)
        return (
            parsed.scheme.lower(),
            parsed.hostname.encode("idna").decode("ascii").lower(),
            port,
            parsed.path.rstrip("/"),
        )
    except (ValueError, UnicodeError):
        return None


def _same_credential_destination(source: dict[str, Any], route: str, base: str) -> bool:
    source_route = str(source.get("route_type") or "custom").strip().lower()
    source_base = (
        OPENROUTER_API_BASE if source_route == "openrouter" else str(source.get("llm_api_base") or "")
    )
    endpoint = _normalized_endpoint(base)
    return source_route == route and endpoint is not None and endpoint == _normalized_endpoint(source_base)


def sanitize_profile(
    payload: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
    copy_source: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate + normalize a profile write. Returns (profile, errors).

    A masked ``llm_api_key`` (or an empty one on update/copy) means "keep the
    stored key" only when the credential destination is unchanged. A copy
    inherits values but always receives a new identity and timestamps.
    OpenRouter profiles have their base pinned to the official
    endpoint and model slugs normalized (outer ``openrouter/`` prefix
    stripped — the engine adds its own provider prefix at spawn time).
    """
    errors: list[str] = []
    existing = existing or {}
    baseline = existing or copy_source or {}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    name = str(payload.get("name", baseline.get("name")) or "").strip()
    if not name:
        errors.append("profile name is required")

    route = str(payload.get("route_type", baseline.get("route_type", "custom")) or "").strip().lower()
    if route not in _ROUTE_TYPES:
        errors.append("route_type must be custom or openrouter")

    base = str(payload.get("llm_api_base", baseline.get("llm_api_base")) or "").strip()
    if route == "openrouter":
        base = OPENROUTER_API_BASE
    else:
        if not base:
            errors.append("API base URL is required for a custom route")
        if base and not re.match(r"^https?://", base, re.IGNORECASE):
            errors.append("API base URL must start with http(s)://")

    key_in = str(payload.get("llm_api_key") or "").strip()
    if is_masked(key_in) or (not key_in and baseline.get("llm_api_key")):
        if baseline.get("llm_api_key") and _same_credential_destination(baseline, route, base):
            key = str(baseline["llm_api_key"])
        else:
            key = ""
            if baseline.get("llm_api_key"):
                errors.append("API endpoint or route changed; enter a new API key")
    else:
        key = key_in
    if not key:
        errors.append("API key is required")

    model_web = strip_provider_prefix(str(payload.get("model_web", baseline.get("model_web")) or ""))
    model_internal = strip_provider_prefix(
        str(payload.get("model_internal", baseline.get("model_internal")) or "")
    )
    model_report = strip_provider_prefix(str(payload.get("model_report", baseline.get("model_report")) or ""))
    if not model_web and not model_internal:
        errors.append("at least one model (web or internal) is required")

    options = {}
    for slot, model in (("web", model_web), ("internal", model_internal), ("report", model_report)):
        api_field = f"api_mode_{slot}"
        effort_field = f"reasoning_effort_{slot}"
        default_api = "auto" if slot == "report" else "chat_completions"
        api_mode = str(payload.get(api_field, baseline.get(api_field, default_api)) or "").strip()
        effort = str(payload.get(effort_field, baseline.get(effort_field, "default")) or "").strip()
        if slot == "report" and not model:
            # No report override means the whole task route is inherited.
            # Disabled report controls must not reject or alter that route.
            api_mode, effort = "auto", "default"
        options[api_field] = api_mode
        options[effort_field] = effort
        errors.extend(f"{slot}: {error}" for error in validate_model_options(model, api_mode, effort))

    profile = {
        "id": str(existing.get("id") or _new_id()),
        "revision": json_store.revision(existing.get("revision")) + 1,
        "name": name,
        "route_type": route,
        "llm_api_base": base,
        "llm_api_key": key,
        "model_web": model_web,
        "model_internal": model_internal,
        "model_report": model_report,
        **options,
        "created_at": str(existing.get("created_at") or now),
        "updated_at": now,
    }
    return profile, errors


def public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """API-safe view: masked key, everything else verbatim."""
    out = dict(profile)
    out["revision"] = json_store.revision(profile.get("revision"))
    out.setdefault("model_report", "")
    for slot in ("web", "internal", "report"):
        out.setdefault(f"api_mode_{slot}", "auto" if slot == "report" else "chat_completions")
        out.setdefault(f"reasoning_effort_{slot}", "default")
    if not str(profile.get("model_report") or "").strip():
        out.update(model_report="", api_mode_report="auto", reasoning_effort_report="default")
    out["llm_api_key"] = mask_key(str(profile.get("llm_api_key") or ""))
    out["llm_api_key_set"] = bool(profile.get("llm_api_key"))
    return out


def effective_llm(profile: dict[str, Any], scan_type: str) -> dict[str, str]:
    """Resolve a profile into the engine spawn env for a scan type."""
    slot = "internal" if scan_type == "internal" else "web"
    if not profile.get(f"model_{slot}"):
        slot = "web" if slot == "internal" else "internal"
    return {
        "llm_api_base": str(profile.get("llm_api_base") or ""),
        "llm_api_key": str(profile.get("llm_api_key") or ""),
        "strix_llm": str(profile.get(f"model_{slot}") or ""),
        "llm_api_mode": str(profile.get(f"api_mode_{slot}") or "chat_completions"),
        "llm_reasoning_effort": str(profile.get(f"reasoning_effort_{slot}") or "default"),
    }


def report_llm_overrides(profile: dict[str, Any]) -> dict[str, str]:
    """Optional report-only model and options; an empty model inherits the task route."""
    model = str(profile.get("model_report") or "").strip()
    return {
        "report_llm": model,
        "report_api_mode": str(profile.get("api_mode_report") or "auto") if model else "auto",
        "report_reasoning_effort": (
            str(profile.get("reasoning_effort_report") or "default") if model else "default"
        ),
    }


def effective_report_llm(profile: dict[str, Any], scan_type: str) -> dict[str, str]:
    """Resolve report model options together using the profile's shared endpoint/key."""
    task = effective_llm(profile, scan_type)
    report = report_llm_overrides(profile)
    if not report["report_llm"]:
        return task
    return {
        **task,
        "strix_llm": report["report_llm"],
        "llm_api_mode": report["report_api_mode"],
        "llm_reasoning_effort": report["report_reasoning_effort"],
    }
