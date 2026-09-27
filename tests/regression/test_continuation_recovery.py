"""Continuation rewrites preserve operator input and exact original references."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from strixops.config.context import ContextSettings
from strixops.engine import continuation_recovery as recovery
from strixops.engine.model_capacity import ModelCapacity
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
from strixops.engine.sessions import open_agent_session, replace_session_items, session_write_lock
from strixops.engine.spawn import _child_initial_input


def make_spec(tmp_path, report=None, credentials=None):
    sources = {
        "previous_report.md": report if report is not None else "PREVIOUS_EVIDENCE\n" * 600,
        "project_credentials.md": credentials if credentials is not None else
        '| db.fixture | `"fixture-user"` | `"fixture|secret\\\\value"` |\n' * 600,
    }
    for name, value in sources.items():
        (tmp_path / name).write_text(value, encoding="utf-8")
    return ScanSpec(
        target="https://current.invalid",
        instruction_text='CURRENT_SCOPE_AND_HINT: keep bytes \\ | ` 中文\n' + PREVIOUS_REPORT_START + "operator example",
        previous_report_file=str(tmp_path / "previous_report.md"),
        continuation={
            "source_run": "fixture-source", "snapshot_file": "previous_report.md",
            "report_sha256": hashlib.sha256(sources["previous_report.md"].encode()).hexdigest(),
        },
        project_credentials_file=str(tmp_path / "project_credentials.md"),
        project_credentials_sha256=hashlib.sha256(sources["project_credentials.md"].encode()).hexdigest(),
    )


@pytest.fixture
def session(tmp_path):
    result = open_agent_session("root", tmp_path / "session.db")
    yield result
    result.close()


@pytest.fixture
def summary(monkeypatch):
    result = AsyncMock(return_value="COMPLETE HISTORICAL SUMMARY")
    monkeypatch.setattr(recovery, "summarize_reference", result)
    return result


async def recover(session, spec, attempt=1):
    return await recovery.recover_continuation(
        session, spec=spec, model="fixture-model", summary_model=object(),
        settings=ContextSettings(), attempt=attempt,
    )


def payload(text, opening=PREVIOUS_REPORT_START, closing=PREVIOUS_REPORT_END):
    return json.loads(text.rsplit(opening, 1)[1].split(closing, 1)[0])


def outside_references(text):
    for opening, closing in (
        (PREVIOUS_REPORT_START, PREVIOUS_REPORT_END),
        (PROJECT_CREDENTIALS_START, PROJECT_CREDENTIALS_END),
    ):
        before, remainder = text.rsplit(opening, 1)
        _, after = remainder.split(closing, 1)
        text = before + after
    return text


@pytest.mark.parametrize("structured", [False, True])
async def test_only_references_change_and_all_hints_and_tool_history_remain_exact(
    tmp_path, session, summary, structured,
):
    spec = make_spec(tmp_path)
    task = build_root_task(spec)
    content = [{"type": "input_text", "text": task}, {"type": "input_image", "image_url": "fixture"}] if structured else task
    history = [
        {"role": "user", "content": content},
        {"role": "user", "content": "LATEST_OPERATOR_HINT\n" + PREVIOUS_REPORT_START},
        {"type": "function_call", "call_id": "done", "name": "fixture_tool", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "done", "output": "VERIFIED_EVIDENCE"},
    ]
    await session.add_items(history)
    original = copy.deepcopy(history)
    report, credentials = spec.load_previous_report(), spec.load_project_credentials()
    assert await recover(session, spec)
    updated = await session.get_items()
    text = updated[0]["content"][0]["text"] if structured else updated[0]["content"]
    assert updated[1:] == original[1:]
    assert outside_references(text) == outside_references(task)
    assert spec.instruction_text in text
    assert len(text.encode()) < len(task.encode())
    if structured:
        assert updated[0]["content"][1:] == original[0]["content"][1:]
    assert payload(text)["summary"] == "COMPLETE HISTORICAL SUMMARY"
    assert payload(text)["read_arguments"]["reference"] == "report"
    credential_ref = payload(text, PROJECT_CREDENTIALS_START, PROJECT_CREDENTIALS_END)
    assert credential_ref["read_arguments"]["reference"] == "credentials"
    assert "markdown" not in credential_ref and "fixture|secret" not in text
    assert spec.load_previous_report() == report and spec.load_project_credentials() == credentials
    summary.assert_awaited_once()
    assert summary.call_args.args == (report,)
    assert history == original


async def test_second_attempt_removes_summary_without_resummarizing_or_changing_instructions(tmp_path, session, summary):
    spec = make_spec(tmp_path)
    task = build_root_task(spec)
    await session.add_items([{"role": "user", "content": task}])
    assert await recover(session, spec)
    first = (await session.get_items())[0]["content"]
    assert await recover(session, spec, attempt=2)
    second = (await session.get_items())[0]["content"]
    assert "summary" not in payload(second)
    assert len(second) < len(first)
    assert outside_references(second) == outside_references(task)
    assert not await recover(session, spec, attempt=2)
    assert not await recover(session, spec, attempt=3)
    summary.assert_awaited_once()


@pytest.mark.parametrize("result", [None, "OVERSIZED SUMMARY" * 10000])
async def test_unusable_summary_falls_back_to_exact_source_reader(tmp_path, session, summary, result):
    summary.return_value = result
    spec = make_spec(tmp_path)
    task = build_root_task(spec)
    await session.add_items([{"role": "user", "content": task}])
    assert await recover(session, spec)
    updated = (await session.get_items())[0]["content"]
    assert "summary" not in payload(updated)
    assert payload(updated)["read_tool"] == "read_continuation_reference"
    assert spec.instruction_text in updated and outside_references(updated) == outside_references(task)


async def test_hints_appended_while_summarizing_are_kept(tmp_path, session, summary):
    spec = make_spec(tmp_path)
    await session.add_items([{"role": "user", "content": build_root_task(spec)}])
    hint = {"role": "user", "content": "NEW_HINT_AFTER_OVERFLOW"}

    async def append_hint(*args, **kwargs):
        async with session_write_lock(session):
            await session.add_items([hint])
        return "COMPLETE SUMMARY"

    summary.side_effect = append_hint
    assert await recover(session, spec)
    assert (await session.get_items())[1:] == [hint]


async def test_changed_opening_item_is_not_overwritten_by_stale_summary(tmp_path, session, summary):
    spec = make_spec(tmp_path)
    await session.add_items([{"role": "user", "content": build_root_task(spec)}])
    newer = [{"role": "user", "content": "CURRENT_REWRITE"}]

    async def rewrite(*args, **kwargs):
        await replace_session_items(session, newer)
        return "COMPLETE SUMMARY"

    summary.side_effect = rewrite
    assert not await recover(session, spec)
    assert await session.get_items() == newer


@pytest.mark.parametrize("source", ["previous_report.md", "project_credentials.md"])
async def test_missing_or_changed_reference_never_replaces_session(tmp_path, session, summary, source):
    spec = make_spec(tmp_path)
    original = [{"role": "user", "content": build_root_task(spec)}]
    await session.add_items(original)
    (tmp_path / source).write_text("CHANGED")
    with pytest.raises(ValueError):
        await recover(session, spec)
    assert await session.get_items() == original


async def test_small_references_are_never_expanded(tmp_path, session, summary):
    spec = make_spec(tmp_path, report="small", credentials="tiny")
    original = [{"role": "user", "content": build_root_task(spec)}]
    await session.add_items(original)
    assert not await recover(session, spec)
    assert await session.get_items() == original


async def test_report_only_continuation_and_cancel(tmp_path, session, summary):
    spec = replace(make_spec(tmp_path), project_credentials_file="", project_credentials_sha256="")
    original = [{"role": "user", "content": build_root_task(spec)}]
    await session.add_items(original)
    summary.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await recover(session, spec)
    assert await session.get_items() == original
    summary.side_effect = None
    assert await recover(session, spec)
    assert PROJECT_CREDENTIALS_START not in (await session.get_items())[0]["content"]


async def test_credentials_marker_in_operator_hint_without_attachment_is_preserved(tmp_path, session, summary):
    spec = replace(make_spec(tmp_path), project_credentials_file="", project_credentials_sha256="")
    example = PROJECT_CREDENTIALS_START + json.dumps({"sha256": "", "markdown": "KEEP_THIS_HINT" * 100}) + PROJECT_CREDENTIALS_END
    spec.instruction_text += "\nOperator example to preserve:\n" + example
    await session.add_items([{"role": "user", "content": build_root_task(spec)}])
    assert await recover(session, spec)
    updated = (await session.get_items())[0]["content"]
    assert spec.instruction_text in updated
    assert example in updated
    assert '"reference": "credentials"' not in updated


async def test_child_inheritance_still_strips_shortened_reference_blocks(tmp_path, session, summary):
    spec = make_spec(tmp_path)
    # Omit the deliberately marker-like operator example in this inheritance test.
    spec.instruction_text = "CURRENT_SCOPE_AND_HINT"
    await session.add_items([{"role": "user", "content": build_root_task(spec)}])
    assert await recover(session, spec)
    child = _child_initial_input(
        EngineServices(spec=spec), "validator", "child", EngineContext(),
        "ASSIGNED_RELEVANT_EXCERPT", await session.get_items(),
    )[0]["content"]
    assert "COMPLETE HISTORICAL SUMMARY" not in child
    assert PROJECT_CREDENTIALS_START.strip() not in child
    assert PREVIOUS_REPORT_START.strip() not in child
    assert "CURRENT_SCOPE_AND_HINT" in child and "ASSIGNED_RELEVANT_EXCERPT" in child


def route_capacity():
    return ModelCapacity(
        model="fixture-model", capacity_tokens=1_000_000, output_limit_tokens=8192,
        capacity_source="provider_metadata", output_source="provider_metadata", lookup_status="resolved",
    )


@pytest.mark.parametrize("body", [
    {"code": "context_length_exceeded", "max_context_length": 200_000},
    {"error": {"code": "context_length_exceeded", "context_window": 200_000}},
    {"error": {"message": "prompt is too long: 210,000 tokens > 200,000 maximum"}},
])
def test_explicit_provider_limit_updates_only_recovery_capacity(body):
    original = route_capacity()
    error = RuntimeError("fixture rejection")
    error.body = body
    recovered = recovery.reported_capacity(error, "fixture-model", original)
    assert recovered.capacity_tokens == 200_000 and recovered.capacity_source == "provider_error"
    assert original.capacity_tokens == 1_000_000


@pytest.mark.parametrize("body", [
    {"code": "context_length_exceeded", "max_tokens": 4096},
    {"code": "invalid_request", "context_window": 4096},
    {"code": "context_length_exceeded", "context_window": True},
    {"code": "context_length_exceeded", "context_window": -1},
])
def test_output_limits_and_unverified_fields_do_not_become_context_capacity(body):
    original = route_capacity()
    error = RuntimeError("fixture rejection")
    error.body = body
    assert recovery.reported_capacity(error, "fixture-model", original) is original
    assert recovery.reported_capacity(error, "different-model", original) is None


def test_explicit_message_limit_without_route_metadata():
    error = RuntimeError("Maximum context length is 8,192 tokens; however, you requested 12000 tokens.")
    recovered = recovery.reported_capacity(error, "fixture-model", None)
    assert recovered.capacity_tokens == 8192 and recovered.model == "fixture-model"
