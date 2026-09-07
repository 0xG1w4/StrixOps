"""Read model catalogs without saving draft profiles or disclosing saved credentials."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from fastapi import HTTPException

from strixops.console import settings_store

REQUEST_TIMEOUT = 12


def _error(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def normalize_api_base(value: str) -> str:
    """Normalize the endpoint identity used to authorize reuse of a stored key."""
    value = value.strip()
    invalid = _error(
        "invalid_api_base", "Enter an HTTP or HTTPS API base URL without credentials, query, or fragment."
    )
    if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise invalid
    if any(char in value for char in ("\\", "?", "#")):
        raise invalid
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            raise invalid
        port = parts.port  # Reject malformed or out-of-range ports before any request.
        host = parts.hostname.encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError) as exc:
        raise invalid from exc
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        authority += f":{port}"
    return urlunsplit((parts.scheme, authority, parts.path.rstrip("/"), "", ""))


def _connection(
    *, route_type: str, llm_api_base: str, llm_api_key: str, profile_id: str | None
) -> tuple[str, str]:
    route = route_type.strip().lower()
    if route not in {"custom", "openrouter"}:
        raise _error("invalid_route", "Choose a custom provider or OpenRouter.")
    base = normalize_api_base(settings_store.OPENROUTER_API_BASE if route == "openrouter" else llm_api_base)
    profile = None
    if profile_id:
        profile = next(
            (item for item in settings_store.load_settings()["profiles"] if item.get("id") == profile_id),
            None,
        )
        if profile is None:
            raise _error("profile_not_found", "The saved profile no longer exists.", 404)

    key = llm_api_key.strip()
    if not key or settings_store.is_masked(key):
        if profile is None:
            raise _error("api_key_required", "Enter an API key to fetch models.")
        saved_route = str(profile.get("route_type") or "custom").strip().lower()
        saved_base = normalize_api_base(
            settings_store.OPENROUTER_API_BASE
            if saved_route == "openrouter"
            else str(profile.get("llm_api_base") or "")
        )
        if saved_route != route or saved_base != base:
            raise _error(
                "endpoint_changed", "The provider or API base changed. Enter an API key for this endpoint."
            )
        key = str(profile.get("llm_api_key") or "").strip()
    if not key or settings_store.is_masked(key):
        raise _error("api_key_required", "Enter an API key to fetch models.")
    return base, key


def _models(payload: Any) -> list[dict[str, str]]:
    invalid = _error("invalid_response", "The provider returned an invalid model catalog.", 502)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise invalid
    if not payload["data"]:
        raise _error("empty_models", "The provider returned no models for this API key.", 502)
    models: dict[str, dict[str, str]] = {}
    for item in payload["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise invalid
        model_id = item["id"]
        name = item.get("name")
        display_name = name.strip() if isinstance(name, str) and name.strip() else model_id
        if model_id not in models:
            models[model_id] = {"id": model_id, "name": display_name}
    return sorted(models.values(), key=lambda item: (item["id"].casefold(), item["id"]))


def discover_models(
    *,
    route_type: str,
    llm_api_base: str,
    llm_api_key: str,
    profile_id: str | None = None,
) -> dict[str, Any]:
    base, key = _connection(
        route_type=route_type, llm_api_base=llm_api_base, llm_api_key=llm_api_key, profile_id=profile_id
    )
    try:
        with requests.Session() as session:
            # A selected provider/key must not be replaced by ambient .netrc credentials.
            session.trust_env = False
            response = session.get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
            status = response.status_code
            if 300 <= status < 400:
                raise _error(
                    "upstream_redirect", "The API base redirected. Enter the provider's direct API base.", 502
                )
            if status in {401, 403}:
                raise _error(
                    "upstream_auth", "The provider rejected this API key or model-list permission.", 502
                )
            if not 200 <= status < 300:
                raise _error(
                    "upstream_http", f"The provider returned HTTP {status} while fetching models.", 502
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise _error(
                    "invalid_response", "The provider returned an invalid model catalog.", 502
                ) from exc
    except requests.Timeout as exc:
        raise _error(
            "upstream_timeout", "The model provider did not respond within 12 seconds.", 504
        ) from exc
    except (requests.RequestException, UnicodeError) as exc:
        raise _error(
            "upstream_connection", "Could not connect to the model provider. Check the API base.", 502
        ) from exc
    models = _models(payload)
    return {"models": models, "count": len(models)}
