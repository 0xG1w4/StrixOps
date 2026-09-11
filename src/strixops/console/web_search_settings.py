"""Safe Console projections of optional search settings and per-run telemetry."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from strixops.config.web_search import MODELS, SearchSettings, from_env
from strixops.console import settings_store

TEST_QUERY = "What does the HTTP 404 status code mean? Cite an official HTTP specification."
_CODES = {
    "ok",
    "disabled",
    "not_configured",
    "invalid_configuration",
    "invalid_query",
    "unauthorized",
    "quota_exceeded",
    "forbidden",
    "rate_limited",
    "cooldown",
    "http_error",
    "upstream_error",
    "network_error",
    "timeout",
    "invalid_response",
    "empty_response",
    "incomplete_response",
    "service_error",
    "cancelled",
}
_COUNTERS = ("calls", "requests", "successes", "failures", "skipped", "input_tokens", "output_tokens")


def effective_settings(data: dict | None = None) -> tuple[SearchSettings, str]:
    integrations = (data if data is not None else settings_store.load_settings()).get("integrations") or {}
    if not isinstance(integrations, dict):
        return SearchSettings(enabled=False, error_code="invalid_configuration"), "none"
    saved = integrations.get("perplexity_api_key", "")
    if not isinstance(saved, str):
        return SearchSettings(enabled=False, error_code="invalid_configuration"), "none"
    saved = saved.strip()
    environment_key = (os.environ.get("PERPLEXITY_API_KEY") or "").strip()
    source = "saved" if saved else "environment" if environment_key else "none"
    values: dict[str, object] = {
        name: value for name, value in os.environ.items() if name.startswith("PERPLEXITY_")
    }
    values["PERPLEXITY_API_KEY"] = saved or environment_key
    for stored, env_name in (
        ("perplexity_enabled", "PERPLEXITY_ENABLED"),
        ("perplexity_model", "PERPLEXITY_MODEL"),
        ("perplexity_timeout_seconds", "PERPLEXITY_TIMEOUT_SECONDS"),
    ):
        if stored in integrations:
            values[env_name] = integrations[stored]
    return from_env(values), source


def public_settings(data: dict | None = None) -> dict:
    settings, source = effective_settings(data)
    return {
        "perplexity_api_key_set": bool(settings.api_key),
        "perplexity_api_key_masked": settings_store.mask_key(settings.api_key),
        "perplexity_enabled": settings.enabled,
        "perplexity_model": settings.model,
        "perplexity_timeout_seconds": settings.timeout_seconds,
        "perplexity_key_source": source,
        "perplexity_error_code": settings.error_code,
    }


def launch_environment() -> dict[str, str]:
    try:
        settings, _ = effective_settings()
    except Exception:
        settings = SearchSettings(enabled=False, error_code="invalid_configuration")
    # Always override inherited values, including an inherited key when search
    # is off. Search configuration failures cannot block the main scan.
    return {
        "PERPLEXITY_ENABLED": "true" if settings.enabled and not settings.error_code else "false",
        "PERPLEXITY_API_KEY": settings.api_key if settings.enabled and not settings.error_code else "",
        "PERPLEXITY_MODEL": settings.model,
        "PERPLEXITY_TIMEOUT_SECONDS": str(settings.timeout_seconds),
    }


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 1_000_000_000_000 or not math.isfinite(value):
        return None
    if integer:
        return value if type(value) is int else None
    return value


def test_diagnostic(result: Any, model: str) -> dict:
    row = result if isinstance(result, dict) else {}
    status = row.get("status") if row.get("status") in ("success", "error", "skipped") else "error"
    code = row.get("code") if isinstance(row.get("code"), str) and row["code"] in _CODES else "service_error"
    return {
        "success": row.get("success") is True and status == "success" and code == "ok",
        "status": status,
        "code": code,
        "model": model if model in MODELS else None,
        "duration_seconds": _number(row.get("duration_seconds")),
        "usage": {
            "input_tokens": _number(row.get("input_tokens"), integer=True),
            "output_tokens": _number(row.get("output_tokens"), integer=True),
            "total_tokens": _number(row.get("total_tokens"), integer=True),
            "cost_usd": _number(row.get("cost_usd")),
        },
    }


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.isoformat() if parsed.tzinfo is not None else None
    except ValueError:
        return None


def read_stats(run_dir: Path) -> dict:
    result: dict[str, Any] = {
        "available": False,
        "code": "not_recorded",
        **dict.fromkeys(_COUNTERS),
        "duration_seconds": None,
        "reported_cost_usd": None,
        "partial_usage": True,
        "circuit_code": None,
        "recent": [],
    }
    try:
        data = _read_stats_file(run_dir)
        if not isinstance(data, dict) or _number(data.get("calls"), integer=True) is None:
            raise ValueError
    except FileNotFoundError:
        return result
    except (OSError, ValueError, RecursionError):
        return {**result, "code": "invalid_stats"}
    result.update(available=True, code="ok")
    result.update({key: _number(data.get(key), integer=True) for key in _COUNTERS})
    result["duration_seconds"] = _number(data.get("duration_seconds"))
    result["reported_cost_usd"] = _number(data.get("reported_cost_usd"))
    result["partial_usage"] = data.get("partial_usage") if type(data.get("partial_usage")) is bool else True
    circuit = data.get("circuit_code")
    result["circuit_code"] = (
        circuit
        if isinstance(circuit, str) and circuit in {"unauthorized", "quota_exceeded", "forbidden"}
        else None
    )
    recent = data.get("recent")
    for row in recent[-20:] if isinstance(recent, list) else []:
        if not isinstance(row, dict):
            continue
        agent = row.get("agent_id")
        result["recent"].append(
            {
                "timestamp": _timestamp(row.get("timestamp")),
                "agent_id": agent
                if isinstance(agent, str) and re.fullmatch(r"root|[0-9a-f]{8}", agent)
                else None,
                "model": row.get("model") if row.get("model") in MODELS else None,
                "status": row.get("status")
                if row.get("status") in ("success", "error", "skipped", "cancelled")
                else None,
                "code": row.get("code")
                if isinstance(row.get("code"), str) and row["code"] in _CODES
                else None,
                "duration_seconds": _number(row.get("duration_seconds")),
                "attempts": _number(row.get("attempts"), integer=True),
                "input_tokens": _number(row.get("input_tokens"), integer=True),
                "output_tokens": _number(row.get("output_tokens"), integer=True),
                "total_tokens": _number(row.get("total_tokens"), integer=True),
                "cost_usd": _number(row.get("cost_usd")),
            }
        )
    return result


def _read_stats_file(run_dir: Path) -> Any:
    """Bound reads and reject symlink/non-regular telemetry, including races."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    run_fd = os.open(run_dir, directory_flags)
    try:
        state_fd = os.open(".state", directory_flags, dir_fd=run_fd)
        try:
            file_fd = os.open("web_search.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=state_fd)
            with os.fdopen(file_fd, "rb") as handle:
                details = os.fstat(handle.fileno())
                if not stat.S_ISREG(details.st_mode) or details.st_size > 128 * 1024:
                    raise ValueError
                raw = handle.read(128 * 1024 + 1)
                if len(raw) > 128 * 1024:
                    raise ValueError
                return json.loads(raw)
        finally:
            os.close(state_fd)
    finally:
        os.close(run_fd)
