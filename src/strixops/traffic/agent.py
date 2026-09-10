"""Independent, bounded HTTP-request Agent runner for MCP TestJobs.

The caller owns capture containers, persistence and the replay policy. This module
owns only its model HTTP client and one SDK run; it never invokes the scan runner.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

import httpx
from agents import Agent, Model, ModelSettings, RunConfig, RunHooks, Runner, StopAtTools, function_tool

from strixops.config.provider import make_platform_model
from strixops.config.settings import EngineSettings
from strixops.engine.stream_cleanup import consume_stream
from strixops.traffic.context import prepare_initial_context
from strixops.traffic.prompts import (
    REQUEST_CONTRACT,
    build_prompt_snapshot,
    compatible_skills,
    public_route,
    resolve_profile,
    validate_snapshot,
)
from strixops.traffic.streaming import StreamMetrics, attach_stream_observer

__all__ = ["build_prompt_snapshot", "compatible_skills", "run_request_test"]

_SENSITIVE = re.compile(
    r"authorization|cookie|password|passwd|passphrase|secret|token|api.?key|credential|session|csrf|nonce",
    re.I,
)
_SECRET_TEXT = re.compile(
    r"""(?i)(["']?(?:password|passwd|passphrase|secret|access_token|refresh_token|token|api[_-]?key|"""
    r"""csrf|nonce|session[_-]?id)["']?\s*[:=]\s*["']?)([^\s"'&,;<>{}]+)"""
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_OUTCOMES = Literal["reported", "no_issue_found", "ruled_out", "not_applicable", "needs_follow_up"]
_SEVERITIES = Literal["critical", "high", "medium", "low", "info"]
_monotonic = time.monotonic


class _WrapUpRequired(Exception):
    """The assessment allowance ended; the hard deadline has not been extended."""


class _IncompleteToolResponse(Exception):
    """A provider ended a tool-producing response without a complete generation."""


class _TimeBudget:
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        self.started = _monotonic()
        self.wrapup_seconds = min(60.0, seconds * 0.3)

    @property
    def elapsed(self) -> float:
        return max(0.0, _monotonic() - self.started)

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed)

    @property
    def assessment_remaining(self) -> float:
        return max(0.0, self.remaining - self.wrapup_seconds)


def _flow_id(flow: dict) -> str:
    return str(flow.get("id") or flow.get("flow_id") or "")


class _Redactor:
    """Keep known captured secrets out of model input, events and result text."""

    def __init__(self) -> None:
        self.secrets: set[str] = set()

    def add(self, value: Any) -> None:
        if isinstance(value, str) and 3 <= len(value) <= 16384 and "redacted" not in value.lower():
            self.secrets.add(value)

    def learn(self, value: Any, key: str = "", depth: int = 0) -> None:
        if depth > 16:
            return
        sensitive = bool(_SENSITIVE.search(key))
        if sensitive:
            self.add(value)
        if isinstance(value, dict):
            if "body_base64" in value:
                from strixops.traffic.body import decode_body

                decoded = decode_body(value).get("body_text")
                if isinstance(decoded, str):
                    self.learn(decoded, "body_text", depth + 1)
            for name, item in value.items():
                if name == "body_base64" and isinstance(item, str) and len(item) <= 2_000_000:
                    with contextlib.suppress(ValueError, UnicodeError):
                        self.learn(base64.b64decode(item).decode("utf-8"), "body", depth + 1)
                else:
                    self.learn(item, key if sensitive else str(name), depth + 1)
        elif isinstance(value, list):
            for item in value:
                if (
                    key == "headers"
                    and isinstance(item, (list, tuple))
                    and len(item) == 2
                    and _SENSITIVE.search(str(item[0]))
                ):
                    header = str(item[1])
                    self.add(header)
                    if header.lower().startswith("bearer "):
                        self.add(header[7:])
                    if "cookie" in str(item[0]).lower():
                        for pair in header.split(";"):
                            if "=" in pair:
                                self.add(pair.split("=", 1)[1].strip())
                else:
                    self.learn(item, key, depth + 1)
        elif isinstance(value, str):
            for match in _JWT.finditer(value):
                self.add(match.group())
            for match in _SECRET_TEXT.finditer(value):
                self.add(match.group(2))
            if key in {"body", "body_text"}:
                with contextlib.suppress(ValueError, RecursionError):
                    parsed = json.loads(value)
                    if isinstance(parsed, (dict, list)):
                        self.learn(parsed, depth=depth + 1)
                for name, item in parse_qsl(value, keep_blank_values=True):
                    if _SENSITIVE.search(name):
                        self.add(item)
            if key == "url":
                with contextlib.suppress(ValueError):
                    parts = urlsplit(value)
                    self.add(parts.password)
                    for name, item in parse_qsl(parts.query):
                        if _SENSITIVE.search(name):
                            self.add(item)

    def text(self, value: Any, limit: int | None = 6000) -> str:
        text = str(value or "")
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [redacted]", text)
        text = _JWT.sub("[redacted]", text)
        text = _SECRET_TEXT.sub(lambda m: m.group(1) + "[redacted]", text)
        return text if limit is None or len(text) <= limit else text[:limit] + "\n[truncated]"

    def value(self, value: Any, key: str = "", depth: int = 0) -> Any:
        if depth > 8:
            return "[truncated nesting]"
        if _SENSITIVE.search(key):
            return "[redacted]"
        if isinstance(value, dict):
            return {
                str(k): self.value(v, str(k), depth + 1)
                for k, v in list(value.items())[:100]
                if k not in {"body_base64", "raw", "raw_request", "raw_response"}
            }
        if isinstance(value, (list, tuple)):
            if key == "headers":
                return [
                    [
                        self.text(pair[0], 100),
                        "[redacted]" if _SENSITIVE.search(str(pair[0])) else self.text(pair[1], 1500),
                    ]
                    for pair in value[:80]
                    if isinstance(pair, (list, tuple)) and len(pair) == 2
                ]
            return [self.value(item, depth=depth + 1) for item in value[:100]]
        if isinstance(value, str):
            return self.text(value)
        return value if value is None or isinstance(value, (bool, int, float)) else self.text(value)


def _model_flow(raw: dict, redactor: _Redactor) -> dict:
    from strixops.traffic.security import public_flow

    redactor.learn(raw)
    public = public_flow(raw, reveal=False)
    allowed = (
        "id",
        "flow_id",
        "method",
        "url",
        "request",
        "response",
        "error",
        "source",
        "parent_flow_id",
        "status_code",
        "created_at",
        "timestamp",
    )
    return redactor.value({key: public[key] for key in allowed if key in public})


def _config(job: dict) -> dict:
    value = job.get("config") or job.get("agent_config") or {}
    if not isinstance(value, dict):
        raise ValueError("TestJob config must be an object")
    return value


async def _cancel_when_requested(check: Callable[[], bool]) -> None:
    while not check():
        await asyncio.sleep(0.05)


async def run_request_test(
    task: dict,
    job: dict,
    flows: list[dict],
    *,
    replay: Callable[[str, dict], Awaitable[dict]],
    emit: Callable[[dict], None],
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    """Execute one TestJob with selected-flow-only tools and explicit completion.

    ``replay`` must enforce persisted task scope, budgets, redirect policy and
    secret handling and return the newly persisted raw Flow. The local checks
    are defense in depth, never an alternative to the executor's network gate.
    """
    snapshot = copy.deepcopy(job.get("prompt_snapshot") or build_prompt_snapshot(task, _config(job)))
    validate_snapshot(snapshot, str(task.get("id") or ""))
    config = snapshot["config"]
    budget = _TimeBudget(config["max_seconds"])
    originals = {_flow_id(flow): flow for flow in flows if _flow_id(flow)}
    if not originals or len(originals) != len(flows) or len(originals) > 50:
        raise ValueError("Select between 1 and 50 distinct persisted flows")
    expected = job.get("flow_ids") or job.get("selected_flow_ids")
    if expected is not None and set(map(str, expected)) != set(originals):
        raise ValueError("Selected flow snapshot does not match this TestJob")
    redactor = _Redactor()
    for flow in originals.values():
        redactor.learn(flow)
    known = dict(originals)
    inspected: set[str] = set()
    findings: list[dict] = []
    coverage: dict[tuple[str, str], dict] = {}
    loaded = set(snapshot["preloaded_skills"])
    request_count = 0
    completed = False
    summary = ""
    stopped = False
    wrapping_up = False
    budget_limited = False
    phase = "initialization"
    model_rounds: list[dict] = []
    replay_lock = asyncio.Lock()

    def progress(kind: str, **data: Any) -> None:
        safe = redactor.value(data)
        # These provider counters contain no token strings. Generic secret-key
        # redaction intentionally masks other fields whose names include token.
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
        ):
            if (
                kind in {"agent.model_started", "agent.model_completed", "agent.model_failed"}
                and key in data
                and (data[key] is None or type(data[key]) is int and data[key] >= 0)
            ):
                safe[key] = data[key]
        emit(
            {
                "type": kind,
                "job_id": str(job.get("id") or ""),
                "phase": phase,
                "stage": "wrapup" if wrapping_up else "assessment",
                "elapsed_seconds": round(budget.elapsed, 3),
                "remaining_seconds": round(budget.remaining, 3),
                **safe,
            }
        )

    def begin_wrapup(reason: str) -> None:
        nonlocal wrapping_up, budget_limited, phase
        if wrapping_up:
            return
        wrapping_up = True
        budget_limited = True
        phase = "wrapup"
        progress("agent.wrapup_started", reason=reason, wrapup_seconds=budget.wrapup_seconds)

    def budget_state() -> dict:
        return {
            "max_seconds": budget.seconds,
            "remaining_seconds": round(budget.remaining, 3),
            "assessment_seconds_remaining": round(budget.assessment_remaining, 3),
            "wrapup_seconds": budget.wrapup_seconds,
            "remaining_requests": max(0, config["max_requests"] - request_count),
            "stage": "wrapup" if wrapping_up else "assessment",
            "instruction": (
                "Stop replaying. Record evidence-backed coverage and finish_request_test now; "
                "unassessed requests must be needs_follow_up."
                if wrapping_up or not budget.assessment_remaining
                else "Reserve the wrap-up allowance for coverage and finish_request_test. "
                "The request limit is a ceiling, not a target to exhaust."
            ),
        }

    def active() -> None:
        if stopped or completed or cancelled():
            raise asyncio.CancelledError()

    def output(value: Any) -> str:
        if isinstance(value, dict):
            value = {**value, "time_budget": budget_state()}
        return json.dumps(redactor.value(value), ensure_ascii=False)

    @function_tool(strict_mode=False, failure_error_function=None)
    def list_selected_requests() -> str:
        """List only this job's selected source requests; use inspect_request for content."""
        active()
        return output(
            {
                "requests": [
                    {
                        "id": identity,
                        "method": row.get("method") or (row.get("request") or {}).get("method"),
                        "url": row.get("url") or (row.get("request") or {}).get("url"),
                    }
                    for identity, row in originals.items()
                ],
                "remaining_requests": max(0, config["max_requests"] - request_count),
            }
        )

    @function_tool(strict_mode=False, failure_error_function=None)
    def inspect_request(flow_id: str) -> str:
        """Read redacted selected or job-generated request/response evidence by stable flow ID."""
        active()
        if flow_id not in known:
            return output({"success": False, "error": "Flow is outside this TestJob"})
        inspected.add(flow_id)
        return output({"success": True, "flow": _model_flow(known[flow_id], redactor)})

    @function_tool(strict_mode=False, failure_error_function=None)
    async def replay_request(flow_id: str, modifications: dict[str, Any] | None = None) -> str:
        """Replay a selection with url (same origin), method, headers, body or body_base64 changes.

        Headers objects merge fields; header lists replace the full list. Redirects are not followed.
        The older proxy tools' params/path/cookies aliases are unavailable. Never send masked secrets.
        """
        nonlocal request_count, phase
        async with replay_lock:
            active()
            if wrapping_up or not budget.assessment_remaining:
                begin_wrapup("assessment_allowance_exhausted")
                return output(
                    {
                        "success": False,
                        "error": "Time reserved for wrap-up; finish coverage without new replays",
                    }
                )
            if flow_id not in originals:
                return output({"success": False, "error": "Replay requires an original selected flow ID"})
            if request_count >= config["max_requests"]:
                return output({"success": False, "error": "Request budget exhausted; finish coverage"})
            changes = modifications or {}
            if set(changes) - {"url", "method", "headers", "body", "body_base64"}:
                return output(
                    {
                        "success": False,
                        "error": "Supported changes: url (same origin), method, headers, body, body_base64",
                    }
                )
            if "[redacted]" in json.dumps(changes, ensure_ascii=False).lower():
                return output({"success": False, "error": "Masked credential values cannot be replayed"})
            request_count += 1
            phase = "replay"
            replay_started = _monotonic()
            progress("agent.request_started", flow_id=flow_id, request_count=request_count)
            try:
                result = await asyncio.wait_for(replay(flow_id, changes), timeout=budget.assessment_remaining)
            except TimeoutError:
                begin_wrapup("replay_reached_wrapup_allowance")
                progress(
                    "agent.request_failed",
                    flow_id=flow_id,
                    error_type="TimeBudgetReached",
                    duration_seconds=round(_monotonic() - replay_started, 3),
                )
                return output({"success": False, "error": "Replay stopped for time-budget wrap-up"})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Exceptions can embed raw requests, tokens and provider URLs.
                progress(
                    "agent.request_failed",
                    flow_id=flow_id,
                    error_type=type(exc).__name__,
                    duration_seconds=round(_monotonic() - replay_started, 3),
                )
                return output(
                    {
                        "success": False,
                        "error": "Request executor rejected or failed the replay",
                        "error_type": type(exc).__name__,
                    }
                )
            active()
            row = result.get("flow") if isinstance(result.get("flow"), dict) else result
            identity = _flow_id(row)
            if not identity or identity in known:
                return output(
                    {"success": False, "error": "Replay did not return new persisted flow evidence"}
                )
            known[identity] = row
            inspected.add(identity)
            public = _model_flow(row, redactor)
            progress(
                "agent.request_completed",
                flow_id=identity,
                parent_flow_id=flow_id,
                duration_seconds=round(_monotonic() - replay_started, 3),
            )
            return output(
                {
                    "success": True,
                    "flow": public,
                    "remaining_requests": max(0, config["max_requests"] - request_count),
                }
            )

    @function_tool(strict_mode=False, failure_error_function=None)
    def list_skills() -> str:
        """List the frozen HTTP-compatible skill subset and unsupported capability limitations."""
        active()
        return output(
            {
                "skills": [
                    {"id": name, "description": row["description"], "loaded": name in loaded}
                    for name, row in snapshot["skills"].items()
                ],
                "limitations": snapshot["limitations"],
            }
        )

    @function_tool(strict_mode=False, failure_error_function=None)
    def load_skill(skills: list[str]) -> str:
        """Load 1–3 canonical HTTP-compatible skills from this TestJob's immutable snapshot."""
        active()
        if not 1 <= len(skills) <= 3 or any(name not in snapshot["skills"] for name in skills):
            return output({"success": False, "error": "Select 1–3 exact IDs from list_skills"})
        requested = list(dict.fromkeys(skills))
        fresh = [name for name in requested if name not in loaded]
        already_loaded = [name for name in requested if name in loaded]
        loaded.update(fresh)
        progress("agent.skills_loaded", skills=fresh, already_loaded=already_loaded)
        # Frozen skill documents are intentionally delivered in full, once. The
        # ordinary evidence-output cap would silently cut their final sections.
        return json.dumps(
            {
                "success": True,
                "loaded_skills": sorted(loaded),
                "already_loaded": already_loaded,
                "skills": [
                    {
                        "id": name,
                        "content": redactor.text(
                            snapshot["skills"][name]["content"],
                            None,
                        ),
                    }
                    for name in fresh
                ],
                "instruction": "Use the skill content already provided; do not reload it for ceremony.",
                "time_budget": budget_state(),
            },
            ensure_ascii=False,
        )

    @function_tool(strict_mode=False, failure_error_function=None)
    def record_coverage(flow_id: str, risk_area: str, outcome: _OUTCOMES, evidence: str) -> str:
        """Record one selected flow/risk conclusion; missing prerequisites are needs_follow_up."""
        active()
        if flow_id not in originals or not risk_area.strip() or not evidence.strip():
            return output({"success": False, "error": "A selected flow, risk area and evidence are required"})
        if flow_id not in inspected:
            return output({"success": False, "error": "Inspect the selected flow before recording coverage"})
        if outcome == "reported" and not any(flow_id in row["evidence_flow_ids"] for row in findings):
            return output({"success": False, "error": "Reported coverage requires a recorded finding"})
        if len(coverage) >= 150 and (flow_id, risk_area) not in coverage:
            return output({"success": False, "error": "Coverage record limit reached"})
        coverage[(flow_id, risk_area)] = {
            "flow_id": flow_id,
            "risk_area": redactor.text(risk_area, 200),
            "outcome": outcome,
            "evidence": redactor.text(evidence),
        }
        return output({"success": True})

    @function_tool(strict_mode=False, failure_error_function=None)
    def create_vulnerability_report(
        title: str,
        severity: _SEVERITIES,
        description: str,
        evidence: str,
        evidence_flow_ids: list[str],
        counterevidence: str,
        impact: str,
        remediation: str,
    ) -> str:
        """Record a validated finding supported by inspected persisted flows and attempted counterevidence."""
        active()
        ids = list(dict.fromkeys(evidence_flow_ids))
        if (
            not ids
            or any(identity not in inspected for identity in ids)
            or not set(ids).intersection(originals)
        ):
            return output(
                {"success": False, "error": "Cite inspected evidence including a selected source flow"}
            )
        if not all(
            value.strip() for value in (title, description, evidence, counterevidence, impact, remediation)
        ):
            return output(
                {"success": False, "error": "Finding, evidence, counterevidence, impact and fix are required"}
            )
        if len(findings) >= 30:
            return output({"success": False, "error": "Finding limit reached"})
        if any(
            row["title"] == redactor.text(title, 300) and row["evidence_flow_ids"] == ids for row in findings
        ):
            return output({"success": False, "error": "This finding is already recorded"})
        finding = {
            "id": f"finding-{len(findings) + 1:04d}",
            "title": redactor.text(title, 300),
            "severity": severity,
            "description": redactor.text(description),
            "evidence": redactor.text(evidence),
            "evidence_flow_ids": ids,
            "counterevidence": redactor.text(counterevidence),
            "impact": redactor.text(impact),
            "remediation": redactor.text(remediation),
            "validation": "agent_verified",
        }
        findings.append(finding)
        progress("agent.finding_recorded", finding_id=finding["id"], severity=severity)
        return output({"success": True, "finding_id": finding["id"]})

    @function_tool(strict_mode=False, failure_error_function=None)
    def finish_request_test(result_summary: str) -> str:
        """Finish this TestJob after recording coverage for every selected source flow."""
        nonlocal completed, summary, budget_limited
        active()
        missing = set(originals) - {row["flow_id"] for row in coverage.values()}
        if not budget.assessment_remaining:
            begin_wrapup("assessment_allowance_exhausted")
        if wrapping_up and result_summary.strip():
            for identity in missing:
                coverage[(identity, "request assessment")] = {
                    "flow_id": identity,
                    "risk_area": "request assessment",
                    "outcome": "needs_follow_up",
                    "evidence": "Time was reserved for wrap-up before this selection was assessed.",
                }
            budget_limited = True
            missing = set()
        if missing or not result_summary.strip():
            return output(
                {
                    "success": False,
                    "missing_coverage": sorted(missing),
                    "error": "Record coverage for every selection and supply a result summary",
                }
            )
        summary = redactor.text(result_summary)
        completed = True
        return output({"success": True, "request_test_completed": True, "summary": summary})

    class TimedModel(Model):
        """Apply this job's deadline around the provider call, including its retries."""

        def __init__(self, wrapped: Model) -> None:
            self.wrapped = wrapped
            self.last_input: Any = None
            self.current_metrics: StreamMetrics | None = None
            self.streams: list[Any] = []
            self.raw_chat = attach_stream_observer(wrapped, lambda: self.current_metrics, self.streams)

        async def get_response(self, *args: Any, **kwargs: Any):
            raise RuntimeError("MCP request tests require the streamed model path")

        async def stream_response(self, *args: Any, **kwargs: Any):
            nonlocal phase
            active()
            if not budget.assessment_remaining:
                begin_wrapup("assessment_allowance_exhausted")
            phase = "model"
            allowance = budget.remaining if wrapping_up else budget.assessment_remaining
            self.last_input = copy.deepcopy(kwargs.get("input", args[1] if len(args) > 1 else None))
            metrics = StreamMetrics(
                self.last_input,
                kwargs.get("system_instructions", args[0] if args else None),
                {tool.name for tool in tools},
                raw_chat=self.raw_chat,
            )
            self.current_metrics = metrics
            number = len(model_rounds) + 1
            started = _monotonic()
            stage = "wrapup" if wrapping_up else "assessment"
            route_metrics = {
                "api_mode": snapshot["model_route"]["llm_api_mode"],
                "reasoning_effort": snapshot["model_route"]["llm_reasoning_effort"],
                "output_limit": None,
            }
            outcome, error_type = "completed", ""
            progress(
                "agent.model_started",
                round=number,
                call_budget_seconds=round(allowance, 3),
                input_chars=metrics.data["input_chars"],
                system_chars=metrics.data["system_chars"],
                streaming=True,
                **route_metrics,
            )
            iterator = self.wrapped.stream_response(*args, **kwargs)
            try:
                async with asyncio.timeout(allowance):
                    async for event in iterator:
                        activity = metrics.observe(event, _monotonic() - started)
                        if activity is not None:
                            progress(
                                "agent.model_streaming",
                                round=number,
                                activity=activity,
                                first_event_seconds=metrics.data["first_event_seconds"],
                                first_output_seconds=metrics.data["first_output_seconds"],
                            )
                        if (
                            getattr(event, "type", None) in {"response.completed", "response.incomplete"}
                            and metrics.data["tools"]
                            and (
                                metrics.data["finish_reason"]
                                in {"length", "incomplete", "content_filter", "failed", "cancelled"}
                                or self.raw_chat
                                and metrics.data["finish_reason"] is None
                            )
                        ):
                            # Even syntactically valid tool arguments from a truncated
                            # generation must not execute or mark a TestJob complete.
                            raise _IncompleteToolResponse()
                        yield event
            except TimeoutError:
                if stage == "assessment" and not budget.assessment_remaining and budget.remaining:
                    outcome, error_type = "wrapup_required", "TimeBudgetReached"
                    begin_wrapup("model_reached_wrapup_allowance")
                    raise _WrapUpRequired() from None
                outcome, error_type = "time_budget_exceeded", "TimeoutError"
                raise
            except asyncio.CancelledError:
                outcome, error_type = "cancelled", "CancelledError"
                raise
            except Exception as exc:
                outcome, error_type = "failed", type(exc).__name__
                raise
            finally:
                cleanup_error: BaseException | None = None
                originating_outcome = outcome
                try:
                    # Both SDK providers can schedule underlying stream close in the
                    # background on cancellation. Join every job-local stream, even
                    # when another close fails, before the HTTP client is closed.
                    closers = [getattr(iterator, "aclose", None)] + [stream.aclose for stream in self.streams]
                    for close in closers:
                        if close is None:
                            continue
                        try:
                            await close()
                        except (Exception, asyncio.CancelledError) as exc:
                            if cleanup_error is None:
                                cleanup_error = exc
                finally:
                    self.streams.clear()
                    self.current_metrics = None
                    if cleanup_error is not None and originating_outcome == "completed":
                        outcome = (
                            "cancelled" if isinstance(cleanup_error, asyncio.CancelledError) else "failed"
                        )
                        error_type = type(cleanup_error).__name__
                    record = {
                        "round": number,
                        "stage": stage,
                        "duration_seconds": round(_monotonic() - started, 3),
                        "outcome": outcome,
                        "error_type": error_type,
                        **metrics.data,
                        **route_metrics,
                    }
                    if cleanup_error is not None:
                        record["cleanup_error_type"] = type(cleanup_error).__name__
                    model_rounds.append(record)
                    progress(
                        "agent.model_completed" if outcome == "completed" else "agent.model_failed", **record
                    )
                if cleanup_error is not None and originating_outcome == "completed":
                    raise cleanup_error

        def get_retry_advice(self, request: Any):
            return self.wrapped.get_retry_advice(request)

        async def _cleanup_on_run_end(self, owner: object) -> None:
            await self.wrapped._cleanup_on_run_end(owner)

    class ToolTiming(RunHooks):
        def __init__(self) -> None:
            self.started: dict[str, float] = {}

        async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
            nonlocal phase
            phase = "tool"
            key = str(getattr(context, "tool_call_id", tool.name))
            self.started[key] = _monotonic()
            progress("agent.tool_started", tool=tool.name, round=len(model_rounds))

        async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
            nonlocal phase
            phase = "tool"
            key = str(getattr(context, "tool_call_id", tool.name))
            started = self.started.pop(key, _monotonic())
            progress(
                "agent.tool_completed",
                tool=tool.name,
                round=len(model_rounds),
                duration_seconds=round(_monotonic() - started, 3),
            )

    tools = [
        list_selected_requests,
        inspect_request,
        replay_request,
        list_skills,
        load_skill,
        record_coverage,
        create_vulnerability_report,
        finish_request_test,
    ]
    progress(
        "agent.started",
        selected_count=len(originals),
        limitations=snapshot["limitations"],
        budget_seconds=budget.seconds,
        wrapup_seconds=budget.wrapup_seconds,
    )
    status = "failed"
    error = ""
    reason = "incomplete_lifecycle"
    runner_task: asyncio.Task | None = None
    cancel_task: asyncio.Task | None = None
    try:
        active()
        _, route = resolve_profile(config["profile_id"])
        if public_route(route) != snapshot["model_route"]:
            raise ValueError("Model profile route changed; create a new TestJob snapshot")
        settings = EngineSettings(
            **route, strix_runs="", operator_hints_dir="", host_workspace_dir="", dry_run=False
        )
        problems = settings.validate()
        if problems:
            raise ValueError("The model route is invalid")

        # An HTTP client belongs to this worker. Concurrent jobs cannot overwrite
        # keys or routing and it is closed after cancellation and on model failure.
        async def bound_transport(request: httpx.Request) -> None:
            allowance = max(0.001, budget.remaining if wrapping_up else budget.assessment_remaining)
            request.extensions["timeout"] = {
                "connect": min(10.0, allowance),
                "read": allowance,
                "write": min(10.0, allowance),
                "pool": min(5.0, allowance),
            }

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(budget.seconds),
            trust_env=False,
            event_hooks={"request": [bound_transport]},
        ) as client:
            model = TimedModel(make_platform_model(settings, http_client=client))

            def instructions(context: Any, current_agent: Any) -> str:
                if not budget.assessment_remaining:
                    begin_wrapup("assessment_allowance_exhausted")
                # Completion needs the mode contract and recorded evidence, not another
                # copy of the full skill corpus on a slow local model's final call.
                prompt = REQUEST_CONTRACT if wrapping_up else snapshot["prompt"]
                if wrapping_up and config["instruction"]:
                    prompt += "\n\nOPERATOR INSTRUCTION\n" + config["instruction"]
                return prompt + "\n\nLIVE TIME BUDGET\n" + json.dumps(budget_state(), ensure_ascii=False)

            agent = Agent(
                name="MCP Request Tester",
                instructions=instructions,
                model=model,
                tools=tools,
                tool_use_behavior=StopAtTools(stop_at_tool_names=["finish_request_test"]),
            )
            initial_context = prepare_initial_context(flows, lambda row: _model_flow(row, redactor))
            inspected.update(initial_context["preinspected_flow_ids"])
            initial_data = redactor.value(
                {
                    "assignment": "Assess only the selected requests using the HTTP tools.",
                    "selected_flow_ids": list(originals),
                    "skill_catalog": [
                        {"id": name, "description": row["description"], "loaded": name in loaded}
                        for name, row in snapshot["skills"].items()
                    ],
                    "loaded_skills": sorted(loaded),
                    "task_id": task.get("id"),
                    "job_id": job.get("id"),
                    "max_requests": config["max_requests"],
                    "max_seconds": config["max_seconds"],
                    "scope_at_job_creation": snapshot.get("scope", {}),
                    "limitations": snapshot["limitations"],
                    "time_budget": budget_state(),
                }
            )
            # This helper already projected/redacted and budgeted the evidence.
            # A second generic truncation pass could invalidate preinspected IDs.
            initial_data["prepared_context"] = initial_context
            initial = json.dumps(initial_data, ensure_ascii=False)

            async def execute() -> None:
                inputs: Any = initial
                corrections = 0
                wrapup_restarted = False
                hooks = ToolTiming()
                while True:
                    active()
                    try:
                        result = Runner.run_streamed(
                            agent,
                            input=inputs,
                            max_turns=min(80, config["max_requests"] * 3 + 16),
                            hooks=hooks,
                            run_config=RunConfig(
                                tracing_disabled=True,
                                trace_include_sensitive_data=False,
                                model_settings=ModelSettings(parallel_tool_calls=False, include_usage=True),
                            ),
                        )
                        # Consume safely without copying raw deltas into persisted events.
                        # The shared cleanup joins SDK producers on every cancellation path.
                        await consume_stream(result, lambda event: None)
                        if getattr(result, "run_loop_exception", None) is not None:
                            raise result.run_loop_exception
                    except _WrapUpRequired:
                        if wrapup_restarted or not budget.remaining:
                            raise TimeoutError() from None
                        wrapup_restarted = True
                        previous = model.last_input or initial
                        inputs = (
                            previous
                            if isinstance(previous, list)
                            else [{"role": "user", "content": previous}]
                        )
                        inputs = [
                            *inputs,
                            {
                                "role": "user",
                                "content": "The assessment allowance ended during the previous model call. "
                                "Use only persisted evidence already returned above. Stop replaying and call "
                                "finish_request_test(result_summary=...) now; missing coverage will be "
                                "retained as needs_follow_up, never a clean result.\n"
                                + output(budget_state()),
                            },
                        ]
                        continue
                    if completed:
                        return
                    if corrections >= 1 or not budget.remaining:
                        return
                    corrections += 1
                    inputs = result.to_input_list() + [
                        {
                            "role": "user",
                            "content": "This TestJob is not complete. Correct the stated missing coverage "
                            "or argument error and call finish_request_test(result_summary=...). Do not "
                            "repeat an unchanged rejected call. Plain text is not completion.\n"
                            + output(budget_state()),
                        }
                    ]

            runner_task = asyncio.create_task(execute())
            cancel_task = asyncio.create_task(_cancel_when_requested(cancelled))
            done, _ = await asyncio.wait(
                {runner_task, cancel_task}, timeout=budget.remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done or cancelled():
                status, error = "cancelled", "Test cancelled"
                reason = "cancelled"
            elif runner_task in done:
                await runner_task
                status = "completed" if completed else "failed"
                if not completed:
                    error = "Agent did not complete the request-test lifecycle"
                else:
                    reason = "time_budget_limited" if budget_limited else "completed"
                    phase = "completion"
            else:
                error = "Time budget exceeded"
                reason = "time_budget_exceeded"
            stopped = True
            for pending in (runner_task, cancel_task):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(runner_task, cancel_task, return_exceptions=True)
    except asyncio.CancelledError:
        status, error = "cancelled", "Test cancelled"
        reason = "cancelled"
    except TimeoutError:
        if not budget.remaining:
            error, reason = "Time budget exceeded", "time_budget_exceeded"
        else:
            error, reason = "Agent model operation timed out", "model_error"
    except _IncompleteToolResponse:
        error = "Model stream ended without a complete tool response"
        reason = "model_error"
    except Exception as exc:
        # Public diagnostics intentionally do not include untrusted exception text.
        error = f"Agent request test failed ({type(exc).__name__})"
        reason = "model_error"
    finally:
        stopped = True
        pending_tasks = [item for item in (runner_task, cancel_task) if item is not None]
        for item in pending_tasks:
            if not item.done():
                item.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
    for identity in originals:
        if not any(row["flow_id"] == identity for row in coverage.values()):
            coverage[(identity, "request assessment")] = {
                "flow_id": identity,
                "risk_area": "request assessment",
                "outcome": "needs_follow_up",
                "evidence": error or "No assessment conclusion was recorded for this selection",
            }
    partial = status != "completed" or budget_limited
    diagnostics = {
        "reason": reason,
        "phase": phase,
        "stage": "wrapup" if wrapping_up else "assessment",
        "elapsed_seconds": round(budget.elapsed, 3),
        "budget_seconds": budget.seconds,
        "remaining_seconds": round(budget.remaining, 3),
        "wrapup_seconds": budget.wrapup_seconds,
        "request_count": request_count,
        "model_rounds": len(model_rounds),
        "partial_evidence": bool(inspected or findings or request_count),
        "rounds": model_rounds,
        "streaming": True,
        "api_mode": snapshot["model_route"]["llm_api_mode"],
        "reasoning_effort": snapshot["model_route"]["llm_reasoning_effort"],
        "output_limit": None,
    }
    progress(
        "agent.finished",
        status=status,
        finding_count=len(findings),
        request_count=request_count,
        reason=reason,
        partial=partial,
    )
    return {
        "status": status,
        "summary": redactor.text(summary or error),
        "error": error,
        "partial": partial,
        "completion_reason": reason,
        "diagnostics": diagnostics,
        "findings": redactor.value(findings),
        "coverage": redactor.value(list(coverage.values())),
        "request_count": request_count,
        "loaded_skills": sorted(loaded),
        "limitations": snapshot["limitations"],
        "prompt_snapshot": snapshot,
    }
