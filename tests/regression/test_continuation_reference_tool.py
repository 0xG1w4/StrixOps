"""Verified continuation attachments remain available through exact bounded reads."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agents.tool_context import ToolContext

from strixops.agents import factory
from strixops.engine.scanconfig import EngineContext, EngineServices, ScanSpec
from strixops.tools.continuation import read_continuation_reference


def continued_spec(
    tmp_path, *, report="Original report\n結論 🔑\n", credentials="| fixture | `\"p\\nword\"` |\n",
):
    report_file = tmp_path / "previous_report.md"
    report_file.write_bytes(report.encode("utf-8"))
    credential_file = tmp_path / "project_credentials.md"
    credential_file.write_bytes(credentials.encode("utf-8"))
    return ScanSpec(
        target="https://fixture.invalid", previous_report_file=str(report_file),
        continuation={
            "source_run": "source", "snapshot_file": "previous_report.md",
            "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
        },
        project_credentials_file=str(credential_file),
        project_credentials_sha256=hashlib.sha256(credentials.encode("utf-8")).hexdigest(),
    )


async def invoke(spec, *, tool=read_continuation_reference, **arguments):
    raw = json.dumps(arguments)
    ctx = ToolContext(
        context=EngineContext(services=EngineServices(spec=spec)), tool_name=tool.name,
        tool_call_id="continuation-test", tool_arguments=raw,
    )
    result = await tool.on_invoke_tool(ctx, raw)
    return json.loads(result)


@pytest.mark.parametrize("reference", ["report", "credentials"])
async def test_pages_reconstruct_original_characters_and_verified_digest(tmp_path, reference):
    original = 'Line one\n🗝️ 引號 " \\ exact\n\x00last\n'
    spec = continued_spec(tmp_path, report=original, credentials=original)
    pages, offset = [], 0
    while True:
        page = await invoke(spec, reference=reference, offset=offset, limit=7)
        assert page["success"] and page["status"] == "ok" and page["source"] == reference
        assert page["sha256"] == hashlib.sha256(original.encode("utf-8")).hexdigest()
        assert page["offset"] == offset and page["total_chars"] == len(original)
        assert page["content"] == original[offset:page["next_offset"]]
        pages.append(page["content"])
        offset = page["next_offset"]
        if not page["has_more"]:
            break
    assert "".join(pages) == original
    eof = await invoke(spec, reference=reference, offset=len(original) + 99)
    assert eof["content"] == "" and not eof["has_more"]
    assert eof["offset"] == eof["next_offset"] == len(original)


async def test_query_is_literal_case_sensitive_from_offset_without_changing_values(tmp_path):
    original = "first HOST key=first\nsecond HOST key=p@ss[]\\word\n"
    spec = continued_spec(tmp_path, credentials=original)
    start = original.index("HOST", 10)
    result = await invoke(spec, reference="credentials", offset=10, limit=12000, query="HOST")
    assert result["offset"] == start and result["content"] == original[start:]
    missing = await invoke(spec, reference="credentials", query="NEVER_ECHO_SECRET")
    assert missing["success"] and missing["status"] == "no_match"
    assert not missing["has_more"] and missing["content"] == ""
    assert missing["next_offset"] == len(original)
    assert "NEVER_ECHO_SECRET" not in json.dumps(missing)
    assert (await invoke(spec, reference="credentials", query="host"))["status"] == "no_match"


@pytest.mark.parametrize("arguments", [
    {"reference": "/private/NEVER_ECHO_SECRET"},
    {"reference": "report", "offset": -1},
    {"reference": "report", "offset": True},
    {"reference": "report", "offset": 1.5},
    {"reference": "report", "limit": 0},
    {"reference": "report", "limit": 12001},
    {"reference": "report", "limit": "NEVER_ECHO_SECRET"},
    {"reference": "report", "query": ["NEVER_ECHO_SECRET"]},
])
async def test_bad_arguments_return_safe_code_before_reading(tmp_path, monkeypatch, arguments):
    spec = continued_spec(tmp_path)
    loader = Mock(side_effect=AssertionError("unexpected file access"))
    monkeypatch.setattr(spec, "load_previous_report", loader)
    monkeypatch.setattr(spec, "load_project_credentials", loader)
    result = await invoke(spec, **arguments)
    assert result == {"success": False, "error_code": "invalid_arguments"}
    loader.assert_not_called()


@pytest.mark.parametrize("spec", [None, ScanSpec(target="https://fresh.invalid")])
async def test_missing_continuation_is_explicit(spec):
    assert await invoke(spec, reference="report") == {
        "success": False, "error_code": "continuation_unavailable",
    }


async def test_optional_absent_credentials_and_changed_snapshot_fail_without_details(tmp_path):
    spec = continued_spec(tmp_path)
    report_only = replace(spec, project_credentials_file="", project_credentials_sha256="")
    assert (await invoke(report_only, reference="credentials"))["error_code"] == "reference_unavailable"
    for reference, filename in (("report", "previous_report.md"), ("credentials", "project_credentials.md")):
        (tmp_path / filename).write_text("CHANGED_SECRET")
        result = await invoke(spec, reference=reference)
        assert result == {"success": False, "error_code": "reference_unavailable"}
        assert "CHANGED_SECRET" not in json.dumps(result) and str(tmp_path) not in json.dumps(result)


async def test_read_and_digest_use_worker_thread_and_exceptions_are_sanitized(tmp_path, monkeypatch):
    spec = continued_spec(tmp_path)
    loop_thread = threading.get_ident()
    seen = []
    original = spec.load_previous_report

    def loader():
        seen.append(threading.get_ident())
        return original()

    monkeypatch.setattr(spec, "load_previous_report", loader)
    assert (await invoke(spec, reference="report"))["success"]
    assert len(seen) == 1 and seen[0] != loop_thread
    monkeypatch.setattr(spec, "load_previous_report", Mock(side_effect=RuntimeError("PRIVATE_PATH SECRET")))
    assert await invoke(spec, reference="report") == {
        "success": False, "error_code": "reference_unavailable",
    }


async def test_output_byte_cap_preserves_json_and_exact_pagination(tmp_path, monkeypatch):
    original = '\x00"\\🔑' * 600
    spec = continued_spec(tmp_path, credentials=original)
    monkeypatch.setenv("STRIX_TOOL_OUTPUT_MAX_BYTES", "1024")
    agent = factory.build_root_agent(spec)
    tool = next(tool for tool in agent.tools if tool.name == "read_continuation_reference")
    pages, offset = [], 0
    while True:
        page = await invoke(spec, tool=tool, reference="credentials", offset=offset, limit=12000)
        assert page["success"] and page["next_offset"] > offset
        assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= 1024
        assert page["content"] == original[offset:page["next_offset"]]
        pages.append(page["content"])
        offset = page["next_offset"]
        if not page["has_more"]:
            break
    assert "".join(pages) == original


@pytest.mark.parametrize("role", ["root", "child"])
def test_factory_adds_tool_only_for_continuation_and_keeps_lifecycle(tmp_path, role):
    spec = continued_spec(tmp_path)

    def build(candidate):
        return (factory.build_root_agent(candidate) if role == "root" else
                factory.build_child_agent("child", "Read assigned historical evidence", spec=candidate))

    fresh = build(ScanSpec(target=spec.target))
    continued = build(spec)
    fresh_names = [tool.name for tool in fresh.tools]
    continued_names = [tool.name for tool in continued.tools]
    assert "read_continuation_reference" not in fresh_names
    assert continued_names == [*fresh_names, "read_continuation_reference"]
    assert ("finish_scan" if role == "root" else "agent_finish") in continued_names
    tool = continued.tools[-1]
    assert getattr(tool, "_strix_bounded", False)
    schema = tool.params_json_schema["properties"]
    assert set(schema) == {"reference", "offset", "limit", "query"}
    assert schema["reference"]["enum"] == ["report", "credentials"]
    assert schema["offset"]["minimum"] == 0
    assert schema["limit"]["minimum"] == 1 and schema["limit"]["maximum"] == 12000
    assert "reversible JSON strings" in tool.description


@pytest.mark.parametrize("role", ["root", "child"])
async def test_factory_uses_frozen_spec_and_preserves_resource_and_bound_wrappers(
    tmp_path, monkeypatch, role,
):
    spec = continued_spec(tmp_path)
    active = []
    bounds = []
    records = []

    @contextmanager
    def activate():
        active.append(True)
        try:
            yield
        finally:
            active.pop()

    resources = SimpleNamespace(
        spec=spec, activate=activate, record_prompt=lambda *args: records.append(args),
    )
    monkeypatch.setattr(factory, "_resources", lambda *_: resources)
    monkeypatch.setattr(factory, "root_instructions", lambda candidate: "fixture")
    monkeypatch.setattr(factory, "child_instructions", lambda *args, **kwargs: "fixture")

    async def bound(result):
        assert active
        bounds.append(result)
        return result

    monkeypatch.setattr(factory, "_bound_result", bound)

    def build(candidate):
        return (factory.build_root_agent(candidate, run_dir=tmp_path) if role == "root" else
                factory.build_child_agent("child", "Review", spec=candidate, run_dir=tmp_path))

    agent = build(ScanSpec(target=spec.target))
    tool = next(tool for tool in agent.tools if tool.name == "read_continuation_reference")
    assert (await invoke(spec, tool=tool, reference="report"))["success"]
    assert len(bounds) == 1 and not active
    assert tool in records[-1][-1]
    resources.spec = ScanSpec(target=spec.target)
    assert "read_continuation_reference" not in [tool.name for tool in build(spec).tools]
