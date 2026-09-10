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
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

import httpx
from agents import Agent, ModelSettings, RunConfig, Runner, StopAtTools, function_tool

from strixops.config.provider import make_platform_model
from strixops.config.settings import EngineSettings
from strixops.traffic.prompts import (
    REQUEST_CONTRACT,
    build_prompt_snapshot,
    compatible_skills,
    public_route,
    resolve_profile,
    validate_snapshot,
)

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


def _flow_id(flow: dict) -> str:
    return str(flow.get("id") or flow.get("flow_id") or "")


class _Redactor:
    """Keep known captured secrets out of model input, events and result text."""

    def __init__(self) -> None:
        self.secrets: set[str] = set()

    def add(self, value: Any) -> None:
        if isinstance(value, str) and 3 <= len(value) <= 16384 and "redacted" not in value.lower():
            self.secrets.add(value)

    def learn(self, value: Any, key: str = "") -> None:
        if _SENSITIVE.search(key):
            self.add(value)
        if isinstance(value, dict):
            for name, item in value.items():
                if name == "body_base64" and isinstance(item, str) and len(item) <= 2_000_000:
                    with contextlib.suppress(ValueError, UnicodeError):
                        self.learn(base64.b64decode(item).decode("utf-8"), "body")
                else:
                    self.learn(item, str(name))
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
                    self.learn(item, key)
        elif isinstance(value, str):
            for match in _JWT.finditer(value):
                self.add(match.group())
            for match in _SECRET_TEXT.finditer(value):
                self.add(match.group(2))
            if key in {"body", "body_text"}:
                with contextlib.suppress(ValueError, RecursionError):
                    parsed = json.loads(value)
                    if isinstance(parsed, (dict, list)):
                        self.learn(parsed)
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

    def text(self, value: Any, limit: int = 6000) -> str:
        text = str(value or "")
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [redacted]", text)
        text = _JWT.sub("[redacted]", text)
        text = _SECRET_TEXT.sub(lambda m: m.group(1) + "[redacted]", text)
        return text if len(text) <= limit else text[:limit] + "\n[truncated]"

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
    replay_lock = asyncio.Lock()

    def progress(kind: str, **data: Any) -> None:
        emit({"type": kind, "job_id": str(job.get("id") or ""), **redactor.value(data)})

    def active() -> None:
        if stopped or completed or cancelled():
            raise asyncio.CancelledError()

    def output(value: Any) -> str:
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
        nonlocal request_count
        async with replay_lock:
            active()
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
            progress("agent.request_started", flow_id=flow_id, request_count=request_count)
            try:
                result = await replay(flow_id, changes)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Exceptions can embed raw requests, tokens and provider URLs.
                progress("agent.request_failed", flow_id=flow_id, error_type=type(exc).__name__)
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
            progress("agent.request_completed", flow_id=identity, parent_flow_id=flow_id)
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
                    {"id": name, "description": row["description"]}
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
        loaded.update(skills)
        progress("agent.skills_loaded", skills=skills)
        return (
            "\n\n".join(
                f"===== SKILL: {name} =====\n{snapshot['skills'][name]['content']}"
                for name in dict.fromkeys(skills)
            )
            + "\n\n"
            + REQUEST_CONTRACT
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
        nonlocal completed, summary
        active()
        missing = set(originals) - {row["flow_id"] for row in coverage.values()}
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
    progress("agent.started", selected_count=len(originals), limitations=snapshot["limitations"])
    status = "failed"
    error = ""
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(45), trust_env=False) as client:
            model = make_platform_model(settings, http_client=client)
            agent = Agent(
                name="MCP Request Tester",
                instructions=snapshot["prompt"],
                model=model,
                tools=tools,
                tool_use_behavior=StopAtTools(stop_at_tool_names=["finish_request_test"]),
            )
            initial = output(
                {
                    "assignment": "Assess only the selected requests using the HTTP tools.",
                    "selected_flow_ids": list(originals),
                    "task_id": task.get("id"),
                    "job_id": job.get("id"),
                    "max_requests": config["max_requests"],
                    "scope_at_job_creation": snapshot.get("scope", {}),
                    "limitations": snapshot["limitations"],
                }
            )

            async def execute() -> None:
                inputs: Any = initial
                for _ in range(3):
                    active()
                    result = await Runner.run(
                        agent,
                        input=inputs,
                        max_turns=min(80, config["max_requests"] * 3 + 16),
                        run_config=RunConfig(
                            tracing_disabled=True,
                            trace_include_sensitive_data=False,
                            model_settings=ModelSettings(parallel_tool_calls=False, max_tokens=2500),
                        ),
                    )
                    if completed:
                        return
                    inputs = result.to_input_list() + [
                        {
                            "role": "user",
                            "content": "This TestJob is not complete. Record missing coverage, then call "
                            "finish_request_test. Plain text or a rejected finish call does not complete it.",
                        }
                    ]

            runner_task = asyncio.create_task(execute())
            cancel_task = asyncio.create_task(_cancel_when_requested(cancelled))
            done, _ = await asyncio.wait(
                {runner_task, cancel_task}, timeout=config["max_seconds"], return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done or cancelled():
                status, error = "cancelled", "Test cancelled"
            elif runner_task in done:
                await runner_task
                status = "completed" if completed else "failed"
                if not completed:
                    error = "Agent did not complete the request-test lifecycle"
            else:
                error = "Time budget exceeded"
            stopped = True
            for pending in (runner_task, cancel_task):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(runner_task, cancel_task, return_exceptions=True)
    except asyncio.CancelledError:
        status, error = "cancelled", "Test cancelled"
    except Exception as exc:
        # Public diagnostics intentionally do not include untrusted exception text.
        error = f"Agent request test failed ({type(exc).__name__})"
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
    progress("agent.finished", status=status, finding_count=len(findings), request_count=request_count)
    return {
        "status": status,
        "summary": redactor.text(summary or error),
        "error": error,
        "findings": redactor.value(findings),
        "coverage": redactor.value(list(coverage.values())),
        "request_count": request_count,
        "loaded_skills": sorted(loaded),
        "limitations": snapshot["limitations"],
        "prompt_snapshot": snapshot,
    }
