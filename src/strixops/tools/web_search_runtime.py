"""Bounded optional Perplexity searches and content-free, run-local diagnostics."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from strixops.config.web_search import SearchSettings

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_RETRY_DELAY_SECONDS = 0.5
_monotonic = time.monotonic

_SECURITY_SYSTEM_PROMPT = """You are assisting a cybersecurity agent specialized in vulnerability
scanning and security assessment running on Kali Linux. When responding to search queries:

1. Prioritize cybersecurity-relevant information including:
   - Vulnerability details (CVEs, CVSS scores, impact)
   - Security tools, techniques, and methodologies
   - Exploit information and proof-of-concepts
   - Security best practices and mitigations
   - Penetration testing approaches

2. Provide technical depth appropriate for security professionals
3. Include specific versions, configurations, and technical details when available
4. Focus on actionable intelligence for security assessment
5. Cite reliable security sources (NIST, OWASP, CVE databases, security vendors)
6. When providing commands, prioritize Kali Linux compatibility
7. Be detailed and specific — include concrete code examples, command-line
   instructions, or configuration snippets when applicable"""

_MESSAGES = {
    "ok": "",
    "disabled": "Web search is disabled. Continue the task without it.",
    "not_configured": "Web search is not configured. Continue the task without it.",
    "invalid_configuration": "Web search configuration is invalid. Continue the task without it.",
    "invalid_query": "Provide a nonempty search query of at most 8000 characters.",
    "unauthorized": "Web search authentication failed; further searches are disabled for this run.",
    "quota_exceeded": "Web search quota or credit is exhausted; further searches are disabled for this run.",
    "forbidden": "Web search access was denied; further searches are disabled for this run.",
    "rate_limited": "Web search is rate limited. Continue the task while search cools down.",
    "cooldown": "Web search is cooling down. Continue the task without another immediate search.",
    "http_error": "The search request was rejected. Continue the task using other evidence.",
    "upstream_error": "The search provider is temporarily unavailable. Continue the task.",
    "network_error": "The search connection failed. Continue the task using other evidence.",
    "timeout": "The search time budget expired. Continue the task using other evidence.",
    "invalid_response": "The search provider returned an invalid response. Continue the task.",
    "empty_response": "Search returned no final answer. Continue the task using other evidence.",
    "incomplete_response": "The search answer was incomplete. Continue the task using other evidence.",
    "service_error": "Web search could not complete. Continue the task using other evidence.",
    "cancelled": "Search cancelled.",
}


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _cost(value: Any) -> float | None:
    if type(value) not in (float, int) or not 0 <= value <= 10**9 or not math.isfinite(value):
        return None
    return float(value)


def _usage(data: Any) -> dict:
    usage = data.get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    cost = usage.get("cost")
    return {
        "input_tokens": _count(usage.get("prompt_tokens")),
        "output_tokens": _count(usage.get("completion_tokens")),
        "total_tokens": _count(usage.get("total_tokens")),
        "cost_usd": _cost(cost.get("total_cost")) if isinstance(cost, dict) else None,
    }


def _empty_stats() -> dict:
    return {
        "calls": 0,
        "requests": 0,
        "successes": 0,
        "failures": 0,
        "skipped": 0,
        "duration_seconds": 0.0,
        "input_tokens": None,
        "output_tokens": None,
        "reported_cost_usd": None,
        "partial_usage": False,
        "circuit_code": None,
        "recent": [],
    }


@dataclass
class SearchState:
    """Shared by root and child agents of one run; no credentials or content stored."""

    run_dir: Path | None = None
    events: Any = None
    circuit_code: str | None = None
    cooldown_until: float = 0.0
    stats: dict = field(default_factory=_empty_stats)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def emit(self, event: str, agent_id: str, payload: dict) -> None:
        with contextlib.suppress(Exception):
            if self.events is not None:
                self.events.emit(event_type=event, agent_id=agent_id, payload=payload)

    def record(self, result: dict, agent_id: str, partial_usage: bool) -> None:
        row = {
            "timestamp": datetime.now(UTC).isoformat(),
            "agent_id": agent_id,
            **{
                key: result[key]
                for key in (
                    "model",
                    "status",
                    "code",
                    "duration_seconds",
                    "attempts",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cost_usd",
                )
            },
        }
        self.stats["calls"] += 1
        self.stats["requests"] += result["attempts"]
        bucket = {"success": "successes", "skipped": "skipped"}.get(result["status"], "failures")
        self.stats[bucket] += 1
        self.stats["duration_seconds"] = round(self.stats["duration_seconds"] + result["duration_seconds"], 3)
        for source, target in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cost_usd", "reported_cost_usd"),
        ):
            if result[source] is not None:
                self.stats[target] = (self.stats[target] or 0) + result[source]
        self.stats["partial_usage"] |= partial_usage
        self.stats["circuit_code"] = self.circuit_code
        self.stats["recent"] = (self.stats["recent"] + [row])[-20:]
        self.emit("web_search.completed", agent_id, row)
        self.persist()

    def persist(self) -> None:
        """A small atomic sidecar write is best effort; run.json is never touched."""
        if self.run_dir is None:
            return
        temporary: str | None = None
        try:
            directory = Path(self.run_dir) / ".state"
            if directory.is_symlink():
                return
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / "web_search.json"
            if destination.is_symlink():
                return
            fd, temporary = tempfile.mkstemp(prefix=".web-search-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.stats, stream, ensure_ascii=False, allow_nan=False)
            os.replace(temporary, destination)
        except Exception:
            pass
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)


def _quota_error(data: Any) -> bool:
    """Recognize explicit machine-readable quota signals, never generic 429 text."""
    if not isinstance(data, dict):
        return False
    error = data.get("error")
    candidates = [data, error] if isinstance(error, dict) else [data]
    return any(
        isinstance(value := item.get(key), str)
        and value.lower()
        in {"insufficient_quota", "quota_exceeded", "insufficient_credits", "credit_balance_exhausted"}
        for item in candidates
        for key in ("code", "type")
    )


def _cooldown(value: str | None) -> float:
    try:
        seconds = float(value or "30")
        return min(300.0, max(1.0, seconds)) if math.isfinite(seconds) else 30.0
    except (TypeError, ValueError):
        return 30.0


def _answer(data: Any, api_key: str) -> tuple[str, list[str], str]:
    if not isinstance(data, dict):
        return "", [], "invalid_response"
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "", [], "invalid_response"
    choice = next((item for item in choices if isinstance(item, dict) and item.get("index", 0) == 0), None)
    if choice is None:
        return "", [], "invalid_response"
    if choice.get("finish_reason") != "stop":
        return "", [], "incomplete_response"
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if content is None or content == "":
        return "", [], "empty_response"
    if not isinstance(content, str):
        return "", [], "invalid_response"
    # Reasoning is not evidence and must not enter the tool result or its logs.
    content = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", content, flags=re.I | re.S)
    if re.search(r"<think\b", content, re.I):
        return "", [], "incomplete_response"
    if re.search(r"</think\s*>", content, re.I):
        content = re.split(r"</think\s*>", content, flags=re.I)[-1]
    content = content.strip()
    if not content:
        return "", [], "empty_response"
    if len(content) > 100_000:
        return "", [], "incomplete_response"
    citations = data.get("citations") or []
    if not isinstance(citations, list) or len(citations) > 200:
        return "", [], "invalid_response"
    for source in citations:
        if not isinstance(source, str) or len(source) > 8192:
            return "", [], "invalid_response"
        try:
            url = urlsplit(source)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                return "", [], "invalid_response"
        except ValueError:
            return "", [], "invalid_response"
    return (
        content.replace(api_key, "[redacted]"),
        [url.replace(api_key, "[redacted]") for url in citations],
        "ok",
    )


async def search(
    query: str, settings: SearchSettings, state: SearchState | None = None, *, agent_id: str = ""
) -> dict:
    """Search once, with at most one transient retry inside one total time budget.

    Non-success is useful tool output, not an exception that terminates a scan.
    Cancellation remains cancellation and closes the active HTTP response/client.
    """
    state = state if state is not None else SearchState()
    started = _monotonic()
    model = settings.model if settings.model in {"sonar", "sonar-reasoning-pro"} else "sonar"
    result = {
        "success": False,
        "status": "error",
        "code": "service_error",
        "continue_task": True,
        "answer": "",
        "citations": [],
        "error": _MESSAGES["service_error"],
        "model": model,
        "duration_seconds": 0.0,
        "attempts": 0,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
    }
    partial_usage = False

    def finish(code: str, status: str = "error") -> dict:
        result.update(success=code == "ok", status=status, code=code, error=_MESSAGES[code])
        return result

    try:
        state.emit("web_search.started", agent_id, {"model": model})
        if getattr(settings, "error_code", None) or settings.model not in {"sonar", "sonar-reasoning-pro"}:
            return finish("invalid_configuration", "skipped")
        if not settings.enabled:
            return finish("disabled", "skipped")
        if not settings.api_key:
            return finish("not_configured", "skipped")
        if not isinstance(query, str) or not query.strip() or len(query) > 8000:
            return finish("invalid_query")
        timeout = settings.timeout_seconds
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            return finish("invalid_configuration", "skipped")
        async with asyncio.timeout(timeout), state.lock:
            if state.circuit_code:
                return finish(state.circuit_code, "skipped")
            if state.cooldown_until > _monotonic():
                return finish("cooldown", "skipped")
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
                for attempt in range(2):
                    result["attempts"] += 1
                    data = None
                    try:
                        async with client.stream(
                            "POST",
                            PERPLEXITY_URL,
                            headers={
                                "Authorization": f"Bearer {settings.api_key}",
                                "Content-Type": "application/json",
                            },
                            json={
                                "model": model,
                                "messages": [
                                    {"role": "system", "content": _SECURITY_SYSTEM_PROMPT},
                                    {"role": "user", "content": query.strip()},
                                ],
                                "stream": False,
                                "temperature": 0.1,
                            },
                        ) as response:
                            status = response.status_code
                            if status in {401, 402, 403}:
                                # Status is sufficient to disable further searches.
                                # Error bodies may be huge, broken, or never finish.
                                partial_usage = True
                                code = {401: "unauthorized", 402: "quota_exceeded", 403: "forbidden"}[status]
                                state.circuit_code = code
                                return finish(code)
                            raw = bytearray()
                            async for chunk in response.aiter_bytes():
                                raw.extend(chunk)
                                if len(raw) > _MAX_RESPONSE_BYTES:
                                    partial_usage = True
                                    return finish("invalid_response")
                            with contextlib.suppress(ValueError, UnicodeError, RecursionError):
                                data = json.loads(raw)
                            usage = _usage(data)
                            partial_usage |= any(value is None for value in usage.values())
                            for key, value in usage.items():
                                if value is not None:
                                    result[key] = (result[key] or 0) + value
                            if _quota_error(data):
                                state.circuit_code = "quota_exceeded"
                                return finish("quota_exceeded")
                            if status == 429:
                                state.cooldown_until = _monotonic() + _cooldown(
                                    response.headers.get("retry-after")
                                )
                                return finish("rate_limited")
                            if 500 <= status <= 599:
                                finish("upstream_error")
                            elif not 200 <= status <= 299:
                                return finish("http_error")
                            else:
                                answer, citations, code = _answer(data, settings.api_key)
                                result.update(answer=answer, citations=citations)
                                return finish(code, "success" if code == "ok" else "error")
                    except httpx.TimeoutException:
                        partial_usage = True
                        if state.circuit_code:
                            return finish(state.circuit_code)
                        finish("timeout")
                    except httpx.RequestError:
                        partial_usage = True
                        if state.circuit_code:
                            return finish(state.circuit_code)
                        finish("network_error")
                    if attempt == 0:
                        await asyncio.sleep(_RETRY_DELAY_SECONDS)
                return result
    except asyncio.CancelledError:
        partial_usage |= result["attempts"] > 0
        finish("cancelled", "cancelled")
        raise
    except TimeoutError:
        partial_usage |= result["attempts"] > 0
        return finish(state.circuit_code or "timeout")
    except Exception:
        partial_usage |= result["attempts"] > 0
        return finish(state.circuit_code or "service_error")
    finally:
        result["duration_seconds"] = round(max(0.0, _monotonic() - started), 3)
        with contextlib.suppress(Exception):
            state.record(result, agent_id, partial_usage)
