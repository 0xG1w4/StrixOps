"""Real SDK and SQLite recovery boundaries with scripted, network-free model responses."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from agents import Agent, Model, ModelResponse, StopAtTools, function_tool
from agents.exceptions import ModelBehaviorError
from agents.items import Usage
from openai import APIError, APIStatusError, BadRequestError
from openai.types.responses import ResponseFunctionToolCall

from strixops.config.context import ContextSettings
from strixops.engine import compaction, continuation_recovery, loop, sessions
from strixops.engine.context_probe import _CONTEXT_CODES
from strixops.engine.coordinator import AgentCoordinator
from strixops.engine.scanconfig import (
    PREVIOUS_REPORT_END,
    PREVIOUS_REPORT_START,
    PROJECT_CREDENTIALS_END,
    PROJECT_CREDENTIALS_START,
    EngineContext,
    EngineServices,
    ScanSpec,
    build_root_task,
)
from strixops.platform.events import EventWriter
from strixops.testing.scripted_gateway import completed_stream_event
from strixops.tools.lifecycle import finish_scan

FINISH = {
    "tool": "finish_scan",
    "arguments": {
        "executive_summary": "Synthetic local test complete",
        "methodology": "In-memory responses only",
        "technical_analysis": "No external systems accessed",
        "recommendations": "None",
    },
}


def overflow():
    return BadRequestError(
        "This model's maximum context length is 50000 tokens; the input is too long.",
        response=httpx.Response(400, request=httpx.Request("POST", "https://model.invalid/v1")),
        body={"code": "context_length_exceeded"},
    )


class ScriptedModel(Model):
    model = "continuation-sdk-fixture"

    def __init__(self, steps):
        self.steps = deque(steps)
        self.inputs = []

    async def get_response(self, *args, **kwargs):
        items = kwargs["input"] if "input" in kwargs else args[1]
        self.inputs.append(copy.deepcopy(items))
        assert self.steps, "Unexpected extra model request"
        step = self.steps.popleft()
        if callable(step):
            step = await step()
        if isinstance(step, BaseException):
            raise step
        index = len(self.inputs)
        output = ResponseFunctionToolCall(
            id=f"fc_{index}", call_id=f"call_{index}", name=step["tool"],
            arguments=json.dumps(step.get("arguments", {})), type="function_call", status="completed",
        )
        return ModelResponse(
            output=[output], response_id=f"response_{index}",
            usage=Usage(requests=1, input_tokens=1, output_tokens=1, total_tokens=2),
        )

    async def stream_response(self, *args, **kwargs):
        yield completed_stream_event(await self.get_response(*args, **kwargs), self.model)


@pytest.fixture
async def environment(tmp_path, monkeypatch):
    report_text = "# ORIGINAL-REPORT\n" + "Every fixture finding must remain retrievable.\n" * 500
    credential_text = "# ORIGINAL-CREDENTIALS\n" + "| fixture.invalid | user | fake-secret |\n" * 200
    report = tmp_path / "previous_report.md"
    credentials = tmp_path / "project_credentials.md"
    report.write_text(report_text)
    credentials.write_text(credential_text)
    spec = ScanSpec(
        target="https://authorized.invalid",
        instruction_text="OPERATOR-EXACT\nPreserve this instruction and current authorized scope.\n",
        previous_report_file=str(report),
        continuation={
            "source_run": "synthetic-source", "snapshot_file": report.name,
            "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        },
        project_credentials_file=str(credentials),
        project_credentials_sha256=hashlib.sha256(credentials.read_bytes()).hexdigest(),
    )
    events = EventWriter(tmp_path)
    coordinator = AgentCoordinator(tmp_path, events)
    await coordinator.register("root", "root", parent_id=None, task="synthetic task")
    session = sessions.open_agent_session("root", tmp_path / "agents.db")
    summary = AsyncMock(return_value="COMPLETE-SYNTHETIC-REPORT-SUMMARY")
    monkeypatch.setattr(continuation_recovery, "summarize_reference", summary)
    recover = AsyncMock(wraps=continuation_recovery.recover_continuation)
    monkeypatch.setattr(continuation_recovery, "recover_continuation", recover)
    compact = AsyncMock(return_value=False)
    monkeypatch.setattr(loop, "_compact_session", compact)
    runner = Mock(wraps=loop.Runner.run_streamed)
    monkeypatch.setattr(loop.Runner, "run_streamed", runner)
    context = EngineContext(
        run_state=Mock(),
        services=EngineServices(
            spec=spec, coordinator=coordinator,
            context_settings=ContextSettings(fallback_context_tokens=1),
        ),
    )
    try:
        yield SimpleNamespace(
            root=tmp_path, spec=spec, events=events, coordinator=coordinator, session=session,
            context=context, summary=summary, recover=recover, compact=compact, runner=runner,
            report_text=report_text, credential_text=credential_text,
        )
    finally:
        session.close()


async def run(environment, model, *, tools=(), initial_input=None, max_turns=20):
    agent = Agent(
        name="root", instructions="Use only the synthetic local tools.", model=model,
        tools=[*tools, finish_scan], tool_use_behavior=StopAtTools(stop_at_tool_names=["finish_scan"]),
    )
    return await loop.run_agent_loop(
        agent, environment.context,
        initial_input=build_root_task(environment.spec) if initial_input is None else initial_input,
        events=environment.events, coordinator=environment.coordinator,
        session=environment.session, max_turns=max_turns,
    )


def reference(items, opening, closing):
    text = items[0]["content"]
    return json.loads(text.rsplit(opening, 1)[1].split(closing, 1)[0])


def without_references(text):
    for opening, closing in (
        (PREVIOUS_REPORT_START, PREVIOUS_REPORT_END),
        (PROJECT_CREDENTIALS_START, PROJECT_CREDENTIALS_END),
    ):
        start = text.rfind(opening)
        end = text.index(closing, start) + len(closing)
        text = text[:start] + text[end:]
    return text


async def test_first_request_keeps_full_references_even_with_many_hints_and_tiny_estimate(environment):
    original = build_root_task(environment.spec)
    hints = [{"role": "user", "content": f"EXACT-OPERATOR-HINT-{number}"} for number in range(7)]
    initial = [{"role": "user", "content": original}, *hints]
    model = ScriptedModel([FINISH])
    assert (await run(environment, model, initial_input=initial))["scan_completed"]
    assert model.inputs == [initial]
    environment.compact.assert_not_awaited()
    environment.recover.assert_not_awaited()
    environment.summary.assert_not_awaited()


async def test_two_overflows_reduce_only_references_and_preserve_each_hint_once(environment):
    original = build_root_task(environment.spec)
    hint = {"role": "user", "content": "EXACT-OPERATOR-HINT\nNo scope expansion."}
    model = ScriptedModel([overflow(), overflow(), FINISH])
    initial = [{"role": "user", "content": original}, hint]
    assert (await run(environment, model, initial_input=initial))["scan_completed"]
    assert model.inputs[0] == initial
    report = reference(model.inputs[1], PREVIOUS_REPORT_START, PREVIOUS_REPORT_END)
    assert report["summary"] == "COMPLETE-SYNTHETIC-REPORT-SUMMARY"
    assert "markdown" not in report
    report = reference(model.inputs[2], PREVIOUS_REPORT_START, PREVIOUS_REPORT_END)
    assert "summary" not in report and "markdown" not in report
    for items in model.inputs[1:]:
        assert without_references(items[0]["content"]) == without_references(original)
        assert items[1:] == [hint]
        credentials = reference(items, PROJECT_CREDENTIALS_START, PROJECT_CREDENTIALS_END)
        assert "markdown" not in credentials
        assert credentials["read_tool"] == "read_continuation_reference"
    assert [call.kwargs["attempt"] for call in environment.recover.await_args_list] == [1, 2]
    assert [call.kwargs["input"] for call in environment.runner.call_args_list] == [[], [], []]
    environment.summary.assert_awaited_once()
    environment.compact.assert_not_awaited()
    assert environment.spec.load_previous_report() == environment.report_text
    assert environment.spec.load_project_credentials() == environment.credential_text


async def test_third_overflow_stops_without_replaying_or_dropping_operator_instructions(environment):
    original = build_root_task(environment.spec)
    model = ScriptedModel([overflow(), overflow(), overflow(), FINISH])
    assert await run(environment, model) is None
    assert len(model.inputs) == environment.runner.call_count == 3
    assert environment.recover.await_count == 2
    assert list(model.steps) == [FINISH]
    assert "after 3 attempts" in environment.context.failure_reason
    saved = await environment.session.get_items()
    assert without_references(saved[0]["content"]) == without_references(original)
    environment.compact.assert_not_awaited()


@pytest.mark.parametrize("kind", ["invalid_request", "authentication", "rate_limit", "output_length"])
async def test_non_context_errors_do_not_shorten_references(environment, kind):
    status = {"invalid_request": 400, "authentication": 401, "rate_limit": 429}.get(kind)
    if kind == "output_length":
        error = ModelBehaviorError("Chat response exceeded max_output_tokens (finish_reason=length).")
    else:
        error_type = BadRequestError if status == 400 else APIStatusError
        error = error_type(
            f"Synthetic {kind}",
            response=httpx.Response(status, request=httpx.Request("POST", "https://model.invalid/v1")),
            body={"code": kind},
        )
    model = ScriptedModel([error, FINISH])
    original = build_root_task(environment.spec)
    result = await run(environment, model, initial_input=original)
    if kind == "rate_limit":
        # The SDK may retry a 429 before producing output. It must resend the
        # identical reference; that transport retry is not context recovery.
        assert result["scan_completed"] and len(model.inputs) == 2
    else:
        assert result is None and len(model.inputs) == 1
    assert all(items == [{"role": "user", "content": original}] for items in model.inputs)
    assert (await environment.session.get_items())[0] == model.inputs[0][0]
    assert environment.runner.call_count == 1
    environment.recover.assert_not_awaited()
    environment.summary.assert_not_awaited()
    environment.compact.assert_not_awaited()


async def test_failed_summary_uses_exact_reader_then_stops_when_references_cannot_shrink(environment):
    environment.summary.return_value = None
    original = build_root_task(environment.spec)
    model = ScriptedModel([overflow(), overflow(), FINISH])
    assert await run(environment, model) is None
    assert len(model.inputs) == 2 and list(model.steps) == [FINISH]
    report = reference(model.inputs[1], PREVIOUS_REPORT_START, PREVIOUS_REPORT_END)
    assert "summary" not in report and "markdown" not in report
    assert report["read_tool"] == "read_continuation_reference"
    assert without_references(model.inputs[1][0]["content"]) == without_references(original)
    assert [call.kwargs["attempt"] for call in environment.recover.await_args_list] == [1, 2]
    environment.summary.assert_awaited_once()
    environment.compact.assert_not_awaited()
    assert environment.spec.load_previous_report() == environment.report_text


async def test_completed_tool_is_saved_once_and_not_replayed_after_overflow(environment):
    executions = []

    @function_tool
    def local_evidence() -> str:
        """Record one synthetic local side effect and return its observation."""
        executions.append("executed")
        return "EXACT-COMPLETED-TOOL-EVIDENCE"

    model = ScriptedModel([{"tool": "local_evidence"}, overflow(), FINISH])
    assert (await run(environment, model, tools=[local_evidence], max_turns=10))["scan_completed"]
    assert executions == ["executed"]
    original_tool_history = model.inputs[1][1:]
    assert [item.get("type") for item in original_tool_history] == ["function_call", "function_call_output"]
    assert original_tool_history[0]["call_id"] == original_tool_history[1]["call_id"]
    assert original_tool_history[1]["output"] == "EXACT-COMPLETED-TOOL-EVIDENCE"
    assert model.inputs[2][1:] == original_tool_history
    assert [call.kwargs["input"] for call in environment.runner.call_args_list] == [[], []]
    assert [call.kwargs["max_turns"] for call in environment.runner.call_args_list] == [10, 9]
    assert environment.recover.await_count == 1
    assert all(not call.kwargs["force"] for call in environment.compact.await_args_list)


async def test_hint_accepted_during_summary_reaches_retry_before_finish(environment):
    hint = "OPERATOR-HINT-DURING-RECOVERY: Preserve this newly accepted instruction."

    async def summarize(*args, **kwargs):
        assert await environment.coordinator.deliver_hint("root", hint)
        return "COMPLETE-SYNTHETIC-REPORT-SUMMARY"

    environment.summary.side_effect = summarize
    model = ScriptedModel([overflow(), FINISH])
    assert (await run(environment, model))["scan_completed"]
    user_contents = [item.get("content") for item in model.inputs[1] if item.get("role") == "user"]
    assert user_contents.count(hint) == 1
    saved = [item.get("content") for item in await environment.session.get_items()
             if item.get("role") == "user"]
    assert saved.count(hint) == 1
    assert not environment.coordinator.pending_messages("root")


@pytest.mark.parametrize("envelope", [False, True])
async def test_native_responses_sse_context_code_recovers_saved_input(environment, envelope):
    detail = {"code": "context_length_exceeded"}
    error = APIError(
        "An error occurred during streaming", request=httpx.Request("POST", "https://model.invalid/v1"),
        body={"error": detail} if envelope else detail,
    )
    model = ScriptedModel([error, FINISH])
    original = build_root_task(environment.spec)
    assert (await run(environment, model))["scan_completed"]
    assert model.inputs[0] == [{"role": "user", "content": original}]
    assert "markdown" not in reference(model.inputs[1], PREVIOUS_REPORT_START, PREVIOUS_REPORT_END)
    assert without_references(model.inputs[1][0]["content"]) == without_references(original)
    environment.recover.assert_awaited_once()


@pytest.mark.parametrize("code", sorted(_CONTEXT_CODES))
@pytest.mark.parametrize("status", [None, 400, 413, 422])
def test_structured_context_codes_accept_only_input_rejection_statuses(code, status):
    request = httpx.Request("POST", "https://model.invalid/v1")
    body = {"error": {"code": code}}
    error = (
        APIError("Unspecified provider error", request=request, body=body) if status is None
        else APIStatusError(
            "Unspecified provider error", response=httpx.Response(status, request=request), body=body,
        )
    )
    assert compaction.is_context_overflow(error)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
@pytest.mark.parametrize("status_location", ["http", "body"])
def test_non_input_http_or_upstream_status_never_becomes_context_recovery(status, status_location):
    request = httpx.Request("POST", "https://model.invalid/v1")
    body = {"error": {"code": "context_length_exceeded"}}
    if status_location == "http":
        error = APIStatusError(
            "context window exceeded", response=httpx.Response(status, request=request), body=body,
        )
    else:
        body["error"]["status_code"] = str(status)
        error = APIError("context window exceeded", request=request, body=body)
    assert not compaction.is_context_overflow(error)


@pytest.mark.parametrize("body", [
    None, {}, {"code": "invalid_request_error"}, {"code": "length"},
    {"error": {"code": "max_output_tokens"}}, {"type": "context_length_exceeded"},
])
def test_generic_api_error_text_and_output_limits_do_not_authorize_context_recovery(body):
    error = APIError(
        "context window exceeded; finish_reason=length",
        request=httpx.Request("POST", "https://model.invalid/v1"), body=body,
    )
    assert not compaction.is_context_overflow(error)
