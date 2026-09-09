"""Bounded, tool-free response diagnostics for saved prompt and skill files."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from agents import ModelBehaviorError, ModelSettings, ModelTracing
from fastapi import APIRouter, HTTPException, Request
from httpx import AsyncClient
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from strixops import skills
from strixops.agents import prompts
from strixops.config.model_errors import (
    UPSTREAM_CYBER_POLICY_MESSAGE,
    model_error_details,
    upstream_policy_code,
)
from strixops.config.model_options import resolved_api_mode, validate_model_options
from strixops.config.provider import strip_provider_prefix
from strixops.config.responses_transport import PlatformResponsesModel
from strixops.config.vision_transport import PlatformChatCompletionsModel
from strixops.console import settings_store
from strixops.console.model_catalog import _connection

router = APIRouter(prefix="/api/prompt-probes", tags=["prompt diagnostics"])

PROBE_VERSION = "3"
TEST_KIND = "task_response"
PROBE_TIMEOUT = 45
PROBE_OUTPUT_TOKENS = 2048
MAX_CONTENT_BYTES = 262144
MAX_TASK_CHARS = 12000
MAX_RESPONSE_CHARS = 32768
MAX_CAPTURE_CHARS = 65536
MAX_CONCURRENT_PROBES = 2
DISCONNECT_POLL_SECONDS = 0.2
_FINGERPRINT_KEY = secrets.token_bytes(32)
_active_probes = 0
_POSSIBLE_REFUSAL = re.compile(
    r"\b(?:i|we)(?:\s+(?:(?:am|are)\s+)?|['’](?:m|re)\s+)"
    r"(?:cannot|can't|can’t|won't|won’t|will not|unable to)\s+"
    r"(?:help|assist|provide|comply|fulfill|fulfil|support|do\s+(?:that|this)|"
    r"execute\s+(?:(?:the|this|that)\s+)?(?:requested\s+)?(?:actions?|request|task))\b|"
    r"\b(?:i(?:'m|’m| am) sorry|(?:i|we) (?:must|have to) (?:refuse|decline)|"
    r"against (?:my|our) (?:policy|policies))\b|"
    r"\b(?:this|that|the) request (?:cannot|can't|can’t|will not|won't|won’t) "
    r"be (?:fulfilled|accepted|supported)\b|"
    r"(?:抱歉|很遺憾|很遗憾|我(?:們|们)?(?:無法|无法|不能|不會|不会)(?:協助|协助|提供|幫助|帮助)|"
    r"我(?:們|们)?(?:無法|无法|不能|不會|不会)(?:執行|执行)(?:這|这|該|该)?(?:項|项)?"
    r"(?:請求|请求|操作|任務|任务)|我(?:們|们)?(?:必須拒絕|必须拒绝)|"
    r"(?:這|这|該|该)(?:項|项)?(?:要求|請求|请求)(?:不予受理|不被接受))",
    re.IGNORECASE,
)


class _ObservedChatStream:
    """Preserve terminal reasons before the SDK projects chat chunks into Responses events."""

    def __init__(self, stream: Any, model: _ChatProbeModel) -> None:
        self.stream = stream
        self.model = model
        self.request_id = getattr(stream, "request_id", None)
        self.response = getattr(stream, "response", None)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        chunk = await anext(self.stream)
        for choice in chunk.choices:
            if choice.index == 0 and choice.finish_reason:
                self.model.finish_reason = choice.finish_reason
        return chunk

    async def aclose(self) -> None:
        await self.stream.close()

    async def close(self) -> None:
        await self.aclose()


class _ChatProbeModel(PlatformChatCompletionsModel):
    finish_reason: str | None = None
    observed_stream: _ObservedChatStream | None = None

    async def _fetch_response(self, *args: Any, **kwargs: Any) -> Any:
        response, stream = await super()._fetch_response(*args, **kwargs)
        self.observed_stream = _ObservedChatStream(stream, self)
        return response, self.observed_stream


class ProbeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["prompt", "skill"]
    name: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_id: str = Field(min_length=1, max_length=200)
    scan_type: Literal["web", "internal"]
    route_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    test_task: str = Field(min_length=1, max_length=MAX_TASK_CHARS)

    @field_validator("test_task")
    @classmethod
    def validate_test_task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a non-empty task to test.")
        return value


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _items() -> dict[tuple[str, str], str]:
    """Resolve only canonical registry entries, excluding escaped symlinks."""
    items: dict[tuple[str, str], str] = {}
    root = prompts.PROMPT_PARTS_DIR.resolve()
    for path in sorted(root.glob("*.md")):
        if not path.resolve().is_relative_to(root) or not path.is_file():
            continue
        try:
            items[("prompt", path.stem)] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    for row in skills.snapshot_skills():
        items[("skill", row["id"])] = row["content"]
    return items


def _routes(settings: dict[str, Any]) -> list[tuple[dict[str, str], dict[str, str]]]:
    routes = []
    for profile in settings["profiles"]:
        profile_id = profile.get("id")
        if not isinstance(profile_id, str) or not profile_id:
            continue
        for scan_type in ("web", "internal"):
            effective = settings_store.effective_llm(profile, scan_type)
            model = strip_provider_prefix(effective["strix_llm"])
            api_mode = effective["llm_api_mode"]
            effort = effective["llm_reasoning_effort"]
            if not model or validate_model_options(model, api_mode, effort):
                continue
            try:
                base, key = _connection(
                    route_type=str(profile.get("route_type") or "custom"),
                    llm_api_base=effective["llm_api_base"],
                    llm_api_key=effective["llm_api_key"],
                    profile_id=None,
                )
            except HTTPException:
                continue
            public = {
                "profile_id": profile_id,
                "profile_name": str(profile.get("name") or profile_id),
                "scan_type": scan_type,
                "model": model,
                "api_mode": resolved_api_mode(model, api_mode),
                "reasoning_effort": effort,
            }
            private = {"base": base, "key": key}
            identity = json.dumps(
                {**public, **private, "route_type": profile.get("route_type")},
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
            public["route_fingerprint"] = hmac.new(_FINGERPRINT_KEY, identity, hashlib.sha256).hexdigest()
            routes.append((public, private))
    return routes


@router.get("/catalog")
def catalog() -> dict[str, Any]:
    settings = settings_store.load_settings()
    return {
        "items": [
            {"kind": kind, "name": name, "sha256": _digest(content), "size": len(content.encode("utf-8"))}
            for (kind, name), content in _items().items()
        ],
        "routes": [public for public, _ in _routes(settings)],
        "active_profile_id": settings["active_profile_id"],
        "max_task_chars": MAX_TASK_CHARS,
        "test_kind": TEST_KIND,
        "probe_version": PROBE_VERSION,
    }


class _ReplyCapture:
    """Retain bounded visible text while checking refusal cues across streamed chunks."""

    def __init__(self) -> None:
        self.text = ""
        self.truncated = False
        self.refusal = False
        self.possible_refusal = False
        self._tail = ""

    def append(self, value: str, *, refusal: bool = False) -> None:
        self.refusal |= refusal
        if not isinstance(value, str):
            return
        self.possible_refusal |= bool(_POSSIBLE_REFUSAL.search(self._tail + value))
        self._tail = (self._tail + value)[-200:]
        remaining = MAX_CAPTURE_CHARS - len(self.text)
        self.text += value[:remaining]
        self.truncated |= len(value) > remaining


def _redact(value: str, key: str, *, truncated: bool = False) -> str:
    """Model text stays plain text; redact the selected credential and common key forms."""
    if truncated and key:
        # A capture boundary can split an echoed credential. Scrub that unfinished
        # suffix before normal replacement, including unusually long saved keys.
        start = value.rfind(key[:32])
        if start >= 0 and key.startswith(value[start:]):
            value = value[:start] + "[redacted]"
        else:
            for length in range(min(31, len(key) - 1, len(value)), 0, -1):
                if value.endswith(key[:length]):
                    value = value[:-length] + "[redacted]"
                    break
    value = value.replace(key, "[redacted]") if key else value
    value = re.sub(r"(?i)Bearer\s+\S+|\bsk[-_][A-Za-z0-9_-]+", "[redacted]", value)
    return "".join(char for char in value if char in "\n\t" or ord(char) >= 32)


def _outcome(
    status: str, code: str, message: str, capture: _ReplyCapture, key: str, diagnostics: str = "",
) -> dict:
    redacted = _redact(capture.text, key, truncated=capture.truncated)
    response_text = redacted[:MAX_RESPONSE_CHARS]
    excerpt = response_text[:1600]
    if code == "possible_refusal" and (match := _POSSIBLE_REFUSAL.search(redacted)):
        start = max(0, match.start() - 200)
        excerpt = (("…" if start else "") + redacted[start:])[:1600]
    return {
        "status": status,
        "code": code,
        "message": message,
        "response_excerpt": excerpt,
        "response_text": response_text,
        "response_truncated": capture.truncated or len(redacted) > MAX_RESPONSE_CHARS,
        "diagnostics": _redact(diagnostics, key)[:1600],
        "test_kind": TEST_KIND,
    }


async def _check(content: str, public: dict[str, str], private: dict[str, str], test_task: str) -> dict:
    """Send the exact saved instruction and user task once, without a Runner or callable tools."""
    key = private["key"]
    completed = None
    terminal_incomplete = False
    capture = _ReplyCapture()
    unexpected_output = False
    saw_text_delta = False
    saw_refusal_delta = False

    def finish(status: str, code: str, message: str, diagnostics: str = "") -> dict:
        return _outcome(status, code, message, capture, key, diagnostics)

    def capture_terminal(response: Any) -> None:
        nonlocal capture, unexpected_output
        terminal = _ReplyCapture()
        for item in response.output:
            if item.type == "message":
                for part in item.content:
                    if part.type in {"refusal", "output_text"}:
                        if terminal.text:
                            terminal.append("\n")
                        terminal.append(
                            part.refusal if part.type == "refusal" else part.text,
                            refusal=part.type == "refusal",
                        )
            elif item.type != "reasoning":
                unexpected_output = True
        if terminal.text.startswith(capture.text):
            terminal.refusal |= capture.refusal
            terminal.possible_refusal |= capture.possible_refusal
            terminal.truncated |= capture.truncated
            capture = terminal
        else:
            # Terminal output is normally cumulative. Keep already streamed text
            # if a gateway instead supplies a shorter or different terminal item.
            capture.refusal |= terminal.refusal
            capture.possible_refusal |= terminal.possible_refusal
            capture.truncated |= terminal.truncated
            if terminal.text and not capture.text.startswith(terminal.text):
                if capture.text:
                    capture.append("\n")
                capture.append(terminal.text)

    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with AsyncClient(
                timeout=PROBE_TIMEOUT, follow_redirects=False, trust_env=False
            ) as http_client:
                async with AsyncOpenAI(
                    base_url=private["base"], api_key=key, http_client=http_client, max_retries=0
                ) as client:
                    model_class = (
                        PlatformResponsesModel
                        if public["api_mode"] == "responses"
                        else _ChatProbeModel
                    )
                    model = model_class(
                        model=public["model"], openai_client=client,
                        reasoning_effort=public["reasoning_effort"],
                    )
                    try:
                        stream = model.stream_response(
                            system_instructions=content,
                            input=[{"role": "user", "content": test_task}],
                            model_settings=ModelSettings(
                                tool_choice="none", parallel_tool_calls=False, include_usage=True,
                                max_tokens=PROBE_OUTPUT_TOKENS, store=False,
                                response_include=(
                                    ["reasoning.encrypted_content"]
                                    if public["api_mode"] == "responses" else None
                                ),
                            ),
                            tools=[], output_schema=None, handoffs=[], tracing=ModelTracing.DISABLED,
                            previous_response_id=None, conversation_id=None, prompt=None,
                        )
                        async with aclosing(stream):
                            async for event in stream:
                                if event.type == "response.refusal.delta":
                                    saw_refusal_delta = True
                                    capture.append(event.delta, refusal=True)
                                elif event.type == "response.refusal.done" and not saw_refusal_delta:
                                    capture.append(event.refusal, refusal=True)
                                elif event.type == "response.output_text.delta":
                                    saw_text_delta = True
                                    capture.append(event.delta)
                                elif event.type == "response.output_text.done" and not saw_text_delta:
                                    capture.append(event.text)
                                elif event.type in {
                                    "response.completed", "response.incomplete", "response.failed",
                                }:
                                    completed = event.response
                                    terminal_incomplete = event.type != "response.completed"
                                    capture_terminal(completed)
                    finally:
                        try:
                            # The SDK schedules chat-stream cleanup on cancellation.
                            # Await that underlying stream here before releasing the HTTP client.
                            if isinstance(model, _ChatProbeModel) and model.observed_stream is not None:
                                await model.observed_stream.aclose()
                        finally:
                            await model.close()
    except (TimeoutError, APITimeoutError):
        return finish("error", "upstream_timeout", "The response did not finish within 45 seconds.")
    except APIError as exc:
        diagnostics = model_error_details(exc)
        if upstream_policy_code(exc):
            return finish("blocked", "cyber_policy", UPSTREAM_CYBER_POLICY_MESSAGE, diagnostics)
        if exc.code == "content_filter":
            return finish("blocked", "content_filter", "The provider explicitly reported a content filter.",
                          diagnostics)
        if isinstance(exc, APIConnectionError):
            return finish("error", "upstream_connection", "Could not connect to the provider.", diagnostics)
        if isinstance(exc, APIStatusError):
            status = exc.status_code
            code = "upstream_auth" if status in {401, 403} else "upstream_http"
            return finish("error", code, f"The provider returned HTTP {status}; no refusal was confirmed.",
                          diagnostics)
        return finish("error", "upstream_stream_error", "The provider reported a stream error; "
                      "no refusal was confirmed.", diagnostics)
    except httpx.HTTPError:
        return finish("error", "upstream_connection", "Could not connect to the provider.")
    except ModelBehaviorError:
        # The SDK raises after yielding response.failed / response.incomplete.
        # Classify their structured terminal details below instead of losing them.
        if not terminal_incomplete:
            return finish("error", "invalid_response", "The provider returned an unsupported response.")
    except Exception:
        # Never expose provider exception strings: they can contain credentials or request bodies.
        return finish("error", "invalid_response", "The provider returned an unsupported response.")

    finish_reason = getattr(model, "finish_reason", None)
    incomplete_reason = getattr(getattr(completed, "incomplete_details", None), "reason", None)
    response_error_code = getattr(getattr(completed, "error", None), "code", None)
    if response_error_code == "cyber_policy":
        return finish("blocked", "cyber_policy", UPSTREAM_CYBER_POLICY_MESSAGE)
    if "content_filter" in {finish_reason, incomplete_reason, response_error_code}:
        return finish("blocked", "content_filter", "The provider explicitly reported a content filter.")
    if capture.refusal:
        return finish("refused", "structured_refusal", "The provider returned a structured refusal.")
    if getattr(completed, "status", None) == "failed" or response_error_code:
        return finish("error", "upstream_response_failed", "The provider reported a failed response; "
                      "no refusal was confirmed.")
    if (
        terminal_incomplete or completed is None or completed.status not in {None, "completed"}
        or (public["api_mode"] == "chat_completions" and finish_reason not in {"stop", "tool_calls"})
    ):
        return finish("inconclusive", "incomplete_response", "The response did not complete.")
    if unexpected_output:
        return finish("inconclusive", "unexpected_output", "The provider returned non-text output; "
                      "no tool was executed.")
    if not _redact(capture.text, key).strip():
        return finish("inconclusive", "empty_response", "The provider returned no visible text.")
    if capture.possible_refusal:
        return finish("inconclusive", "possible_refusal", "The text may contain a refusal. "
                      "Review the reply; this is not a confirmed policy block.")
    return finish("responded", "task_response", "No refusal signal detected in this response. "
                  "Review the reply for task completion.")


@router.post("/run")
async def run_probe(body: ProbeBody, request: Request) -> dict[str, Any]:
    global _active_probes
    content = _items().get((body.kind, body.name))
    if content is None:
        raise _error(404, "item_not_found", "The saved prompt or skill no longer exists.")
    if _digest(content) != body.sha256:
        raise _error(409, "content_changed", "The saved content changed. Refresh the catalog before testing.")
    if not content.strip():
        raise _error(400, "empty_content", "The saved prompt is empty. Add content before testing.")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise _error(413, "content_too_large", "The saved content exceeds the diagnostic input limit.")
    match = next((
        (public, private)
        for public, private in _routes(settings_store.load_settings())
        if public["profile_id"] == body.profile_id and public["scan_type"] == body.scan_type
    ), None)
    if match is None or not hmac.compare_digest(match[0]["route_fingerprint"], body.route_fingerprint):
        raise _error(409, "route_changed", "The saved route changed or is unavailable. Refresh the catalog.")
    if _active_probes >= MAX_CONCURRENT_PROBES:
        raise _error(429, "probe_busy", "Two diagnostics are already running. Wait before starting another.")
    public, private = match
    _active_probes += 1
    started = time.monotonic()
    probe = asyncio.create_task(_check(content, public, private, body.test_task))
    try:
        while not probe.done():
            done, _ = await asyncio.wait({probe}, timeout=DISCONNECT_POLL_SECONDS)
            if done:
                break
            if await request.is_disconnected():
                raise _error(499, "probe_canceled", "The diagnostic was canceled.")
        result = await probe
        return {
            **body.model_dump(),
            **{name: public[name] for name in ("model", "api_mode", "reasoning_effort")},
            **result,
            "checked_at": datetime.now(UTC).isoformat(),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "probe_version": PROBE_VERSION,
            "task_sha256": _digest(body.test_task),
        }
    finally:
        if not probe.done():
            probe.cancel()
        try:
            await asyncio.gather(probe, return_exceptions=True)
        finally:
            _active_probes -= 1
