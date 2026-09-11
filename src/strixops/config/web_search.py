"""Fail-closed optional web-search configuration, independent of model routes."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

MODELS = ("sonar", "sonar-reasoning-pro")


@dataclass(frozen=True)
class SearchSettings:
    enabled: bool = True
    api_key: str = field(default="", repr=False)
    model: str = "sonar"
    timeout_seconds: float = 30.0
    error_code: str | None = None


def from_env(environ: Mapping[str, object] | None = None) -> SearchSettings:
    """Read environment values without raising or exposing invalid credentials.

    Bad legacy values disable only this optional integration. They must never
    prevent a Web/Internal scan from starting. No storage is read or rewritten.
    """
    values = os.environ if environ is None else environ
    key = values.get("PERPLEXITY_API_KEY", "")
    invalid_key_type = not isinstance(key, str)
    key = key.strip() if isinstance(key, str) else ""
    try:
        if invalid_key_type:
            raise ValueError
        enabled_value = values.get("PERPLEXITY_ENABLED", True)
        if isinstance(enabled_value, bool):
            enabled = enabled_value
        elif isinstance(enabled_value, str) and enabled_value.strip().lower() in {
            "true",
            "false",
            "1",
            "0",
            "yes",
            "no",
            "on",
            "off",
        }:
            enabled = enabled_value.strip().lower() in {"true", "1", "yes", "on"}
        else:
            raise ValueError
        model = values.get("PERPLEXITY_MODEL", "sonar")
        if not isinstance(model, str) or model.strip() not in MODELS:
            raise ValueError
        model = model.strip()
        timeout_value = values.get(
            "PERPLEXITY_TIMEOUT_SECONDS", 300 if model == "sonar-reasoning-pro" else 30
        )
        if isinstance(timeout_value, bool):
            raise ValueError
        timeout = float(timeout_value)
        if not math.isfinite(timeout) or not 10 <= timeout <= 300:
            raise ValueError
        if len(key) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in key):
            raise ValueError
        return SearchSettings(enabled=enabled, api_key=key, model=model, timeout_seconds=timeout)
    except (TypeError, ValueError, OverflowError):
        return SearchSettings(enabled=False, api_key=key, error_code="invalid_configuration")
