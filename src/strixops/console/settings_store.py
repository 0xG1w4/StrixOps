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

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

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


def load_settings() -> dict[str, Any]:
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
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
            "perplexity_api_key": str(integrations.get("perplexity_api_key") or ""),
        },
    }


def save_settings(settings: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


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


def sanitize_profile(
    payload: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate + normalize a profile write. Returns (profile, errors).

    A masked ``llm_api_key`` (or an empty one on update) means "keep the
    stored key". OpenRouter profiles have their base pinned to the official
    endpoint and model slugs normalized (outer ``openrouter/`` prefix
    stripped — the engine adds its own provider prefix at spawn time).
    """
    errors: list[str] = []
    existing = existing or {}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    name = str(payload.get("name") or existing.get("name") or "").strip()
    if not name:
        errors.append("profile name is required")

    route = str(payload.get("route_type") or existing.get("route_type") or "custom").strip().lower()
    if route not in _ROUTE_TYPES:
        errors.append("route_type must be custom or openrouter")

    base = str(payload.get("llm_api_base") or "").strip()
    if route == "openrouter":
        base = OPENROUTER_API_BASE
    else:
        if not base and not existing.get("llm_api_base"):
            errors.append("API base URL is required for a custom route")
        base = base or str(existing.get("llm_api_base") or "")
        if base and not re.match(r"^https?://", base):
            errors.append("API base URL must start with http(s)://")

    key_in = str(payload.get("llm_api_key") or "").strip()
    if is_masked(key_in) or (not key_in and existing.get("llm_api_key")):
        key = str(existing.get("llm_api_key") or "")
    else:
        key = key_in
    if not key:
        errors.append("API key is required")

    model_web = strip_provider_prefix(str(payload.get("model_web") or existing.get("model_web") or ""))
    model_internal = strip_provider_prefix(
        str(payload.get("model_internal") or existing.get("model_internal") or "")
    )
    if not model_web and not model_internal:
        errors.append("at least one model (web or internal) is required")

    profile = {
        "id": str(existing.get("id") or _new_id()),
        "name": name,
        "route_type": route,
        "llm_api_base": base,
        "llm_api_key": key,
        "model_web": model_web,
        "model_internal": model_internal,
        "created_at": str(existing.get("created_at") or now),
        "updated_at": now,
    }
    return profile, errors


def public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """API-safe view: masked key, everything else verbatim."""
    out = dict(profile)
    out["llm_api_key"] = mask_key(str(profile.get("llm_api_key") or ""))
    out["llm_api_key_set"] = bool(profile.get("llm_api_key"))
    return out


def effective_llm(profile: dict[str, Any], scan_type: str) -> dict[str, str]:
    """Resolve a profile into the engine spawn env for a scan type."""
    if scan_type == "internal":
        model = profile.get("model_internal") or profile.get("model_web") or ""
    else:
        model = profile.get("model_web") or profile.get("model_internal") or ""
    return {
        "llm_api_base": str(profile.get("llm_api_base") or ""),
        "llm_api_key": str(profile.get("llm_api_key") or ""),
        "strix_llm": str(model),
    }
