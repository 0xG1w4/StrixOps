"""Reporting parity regressions, exercising SDK tools and persisted findings."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agents import ModelResponse, ModelTracing, RunContextWrapper
from agents.tool_context import ToolContext
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from strixops.engine.scanconfig import EngineContext, EngineServices
from strixops.engine.usage import UsageAccumulator, UsageTrackingModel
from strixops.platform.events import EventWriter
from strixops.report import dedupe
from strixops.report.cvss import CVSS_VALID, calculate_cvss, validate_cvss_breakdown
from strixops.report.state import RunState
from strixops.tools import reporting

CRITICAL = {
    "attack_vector": "N",
    "attack_complexity": "L",
    "privileges_required": "N",
    "user_interaction": "N",
    "scope": "U",
    "confidentiality": "H",
    "integrity": "H",
    "availability": "H",
}


def _response(text: str) -> ModelResponse:
    return ModelResponse(
        output=[
            ResponseOutputMessage(
                type="message",
                id="dedupe-response",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
            )
        ],
        usage=Usage(requests=1, input_tokens=100, output_tokens=20, total_tokens=120),
        response_id="dedupe-response",
    )


def _verdict(duplicate: bool = False) -> str:
    return json.dumps(
        {
            "is_duplicate": duplicate,
            "duplicate_id": "vuln-0001" if duplicate else "",
            "confidence": 0.95,
            "reason": "Same root cause and username parameter" if duplicate else "Different endpoints",
        }
    )


def _context(tmp_path: Path, model=None) -> RunContextWrapper[EngineContext]:
    state = RunState(tmp_path, EventWriter(tmp_path))
    services = EngineServices(model_for=Mock(return_value=model) if model is not None else None)
    return RunContextWrapper(EngineContext(run_state=state, services=services))


def _report_args(**overrides) -> dict:
    args = {
        "title": "SQL injection",
        "description": "SQL injection in username at the login endpoint",
        "impact": "Database contents can be read",
        "target": "https://example.test",
        "technical_analysis": "Unparameterized query using username",
        "poc_description": "A controlled quote payload changes the query result",
        "poc_script_code": "print('local fixture')",
        "remediation_steps": "Use a parameterized query",
        "evidence": "The local fixture returned the expected controlled value",
        "assumptions": "Unauthenticated access to the form",
        "counterevidence": "Other form fields do not alter the query",
        "confidence": "high",
        "severity_change_conditions": "An authentication boundary would reduce exposure",
        "fix_effort": "low",
        "cvss_breakdown": dict(CRITICAL),
        "endpoint": "/login",
        "method": "POST",
    }
    args.update(overrides)
    return args


async def _create(ctx, **overrides) -> dict:
    arguments = json.dumps(_report_args(**overrides))
    tool_ctx = ToolContext.from_agent_context(
        ctx, "create-report", tool_name="create_vulnerability_report", tool_arguments=arguments
    )
    result = await reporting.create_vulnerability_report.on_invoke_tool(tool_ctx, arguments)
    return json.loads(result)


async def _update(ctx, **overrides) -> dict:
    args = {"report_id": "vuln-0001", "update_reason": "Validated revised impact", **overrides}
    arguments = json.dumps(args)
    tool_ctx = ToolContext.from_agent_context(
        ctx, "update-report", tool_name="update_vulnerability_report", tool_arguments=arguments
    )
    result = await reporting.update_vulnerability_report.on_invoke_tool(tool_ctx, arguments)
    return json.loads(result)


def _files(tmp_path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


async def test_canonical_critical_vector_is_filed_with_matching_score(tmp_path):
    model = SimpleNamespace(get_response=AsyncMock())
    ctx = _context(tmp_path, model)
    result = await _create(ctx)
    assert result["success"] is True
    assert result["severity"] == "critical"
    assert result["cvss_score"] == 9.8
    report = ctx.context.run_state.reports[0]
    assert report["cvss"] == 9.8
    assert report["cvss_breakdown"] == CRITICAL
    assert report["cvss_vector"] == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    ctx.context.services.model_for.assert_not_called()
    model.get_response.assert_not_awaited()


@pytest.mark.parametrize("metric", CVSS_VALID)
@pytest.mark.parametrize("invalid", ["missing", "illegal"])
async def test_invalid_metric_rejects_create_before_persistence(tmp_path, metric, invalid):
    ctx = _context(tmp_path)
    breakdown = dict(CRITICAL)
    if invalid == "missing":
        del breakdown[metric]
    else:
        breakdown[metric] = "invalid"
    before = _files(tmp_path)
    result = await _create(ctx, cvss_breakdown=breakdown)
    assert result["success"] is False
    assert any(metric in error for error in result["errors"])
    assert ctx.context.run_state.reports == []
    assert _files(tmp_path) == before


@pytest.mark.parametrize("breakdown", [None, [], {}, {"Attack Vector": "N"}, {**CRITICAL, "scope": "F"}])
def test_cvss_input_shape_and_old_noncanonical_format_are_rejected(breakdown):
    assert validate_cvss_breakdown(breakdown)


def test_zero_score_maps_to_info_like_reference():
    breakdown = {**CRITICAL, "confidentiality": "N", "integrity": "N", "availability": "N"}
    score, severity, vector = calculate_cvss(breakdown)
    assert (score, severity) == (0.0, "info")
    assert vector.endswith("C:N/I:N/A:N")


@pytest.mark.parametrize("breakdown", [{}, {"attack_vector": "N"}, {**CRITICAL, "scope": "F"}])
async def test_invalid_revision_preserves_entire_report_and_artifacts(tmp_path, breakdown):
    ctx = _context(tmp_path)
    await _create(ctx)
    before_report = copy.deepcopy(ctx.context.run_state.reports[0])
    before_files = _files(tmp_path)
    result = await _update(ctx, title="Must not be applied", cvss_breakdown=breakdown)
    assert result["success"] is False
    assert result["error"] == "Validation failed"
    assert ctx.context.run_state.reports[0] == before_report
    assert _files(tmp_path) == before_files


async def test_valid_revision_replaces_rating_together_and_records_prior_values(tmp_path):
    ctx = _context(tmp_path)
    await _create(ctx)
    breakdown = {**CRITICAL, "confidentiality": "L", "integrity": "N", "availability": "N"}
    result = await _update(ctx, cvss_breakdown=breakdown)
    assert result["success"] is True
    assert (result["severity"], result["cvss_score"]) == ("medium", 5.3)
    report = ctx.context.run_state.reports[0]
    assert (report["cvss"], report["severity"]) == (5.3, "medium")
    assert report["cvss_breakdown"] == breakdown
    assert report["cvss_vector"].endswith("C:L/I:N/A:N")
    prior = report["update_history"][-1]["previous"]
    assert (prior["cvss"], prior["severity"]) == (9.8, "critical")
    assert prior["cvss_breakdown"] == CRITICAL


async def test_text_only_revision_keeps_rating(tmp_path):
    ctx = _context(tmp_path)
    await _create(ctx)
    result = await _update(ctx, evidence="Reproduced again")
    assert result["success"] is True
    assert result["cvss_score"] == 9.8
    assert ctx.context.run_state.reports[0]["severity"] == "critical"


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_scoring_failure_does_not_mutate_reports(tmp_path, monkeypatch, operation):
    ctx = _context(tmp_path)
    if operation == "update":
        await _create(ctx)
    before_reports = copy.deepcopy(ctx.context.run_state.reports)
    before_files = _files(tmp_path)
    monkeypatch.setattr(reporting, "calculate_cvss", Mock(side_effect=ValueError("Cannot score vector")))
    result = await (_create(ctx) if operation == "create" else _update(ctx, cvss_breakdown=CRITICAL))
    assert result["success"] is False
    assert result["errors"] == ["Cannot score vector"]
    assert ctx.context.run_state.reports == before_reports
    assert _files(tmp_path) == before_files


@pytest.mark.parametrize("difference", ["endpoint", "parameter", "target"])
async def test_same_title_cve_or_cwe_does_not_bypass_model_comparison(tmp_path, difference):
    model = SimpleNamespace(get_response=AsyncMock(return_value=_response(_verdict(False))))
    ctx = _context(tmp_path, model)
    await _create(ctx, cve="CVE-2026-1000", cwe="CWE-89")
    changes = {
        "endpoint": {"endpoint": "/search"},
        "parameter": {"technical_analysis": "Different unparameterized query using email"},
        "target": {"target": "https://other.example.test"},
    }[difference]
    result = await _create(ctx, cve="CVE-2026-1000", cwe="CWE-89", **changes)
    assert result["success"] is True
    assert len(ctx.context.run_state.reports) == 2
    ctx.context.services.model_for.assert_called_once_with("dedupe")
    call = model.get_response.await_args.kwargs
    assert "Different endpoints" in call["system_instructions"]
    assert "Different parameters" in call["system_instructions"]
    assert "Same root cause" in call["system_instructions"]
    comparison = json.loads(call["input"].split("\n\n")[1])
    for key, value in changes.items():
        assert comparison["candidate"][key] == value
    assert comparison["existing_reports"][0]["id"] == "vuln-0001"
    assert "cve" not in comparison["candidate"]
    assert call["tools"] == []
    assert call["model_settings"].include_usage is True
    assert call["model_settings"].parallel_tool_calls is None
    assert call["tracing"] == ModelTracing.DISABLED


async def test_confirmed_duplicate_returns_reference_details_without_mutation(tmp_path):
    model = SimpleNamespace(get_response=AsyncMock(return_value=_response(_verdict(True))))
    ctx = _context(tmp_path, model)
    await _create(ctx)
    before_reports = copy.deepcopy(ctx.context.run_state.reports)
    before_files = _files(tmp_path)
    result = await _create(ctx, title="SQL injection through login username")
    assert result["success"] is False
    assert result["is_duplicate"] is True
    assert result["duplicate_id"] == result["duplicate_of"] == "vuln-0001"
    assert result["duplicate_title"] == "SQL injection"
    assert result["confidence"] == 0.95
    assert "username" in result["reason"]
    assert ctx.context.run_state.reports == before_reports
    assert _files(tmp_path) == before_files


@pytest.mark.parametrize("response", ["", "no JSON", "{invalid JSON}"])
async def test_empty_or_unparseable_dedupe_does_not_drop_finding(tmp_path, response):
    model = SimpleNamespace(get_response=AsyncMock(return_value=_response(response)))
    ctx = _context(tmp_path, model)
    await _create(ctx)
    assert (await _create(ctx))["success"] is True
    assert len(ctx.context.run_state.reports) == 2


@pytest.mark.parametrize("failure", ["request", "resolution", "unconfigured"])
async def test_unavailable_dedupe_model_fails_open(tmp_path, failure):
    model = SimpleNamespace(get_response=AsyncMock(side_effect=RuntimeError("model unavailable")))
    ctx = _context(tmp_path, model if failure != "unconfigured" else None)
    if failure == "resolution":
        ctx.context.services.model_for.side_effect = RuntimeError("route unavailable")
    await _create(ctx)
    assert (await _create(ctx))["success"] is True
    assert len(ctx.context.run_state.reports) == 2


async def test_dedupe_uses_existing_tracked_model_and_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT", "42")
    inner = SimpleNamespace(get_response=AsyncMock(return_value=_response(_verdict())))
    usage = UsageAccumulator()
    model = UsageTrackingModel(inner, usage)
    ctx = _context(tmp_path, model)
    await _create(ctx)
    await _create(ctx, endpoint="/search")
    assert usage.snapshot() == {
        "requests": 1,
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
    }
    settings = inner.get_response.await_args.args[2]
    assert settings.extra_args == {"timeout": 42.0}


def test_dedupe_parser_accepts_reference_fences_and_normalizes_fields():
    result = dedupe._parse_dedupe_response(
        '```json\n{"is_duplicate": true, "duplicate_id": "vuln-0001", '
        '"confidence": "0.95", "reason": "Same root cause"}\n```'
    )
    assert result == {
        "is_duplicate": True,
        "duplicate_id": "vuln-0001",
        "confidence": 0.95,
        "reason": "Same root cause",
    }


def test_dedupe_comparison_preserves_location_and_bounds_large_analysis():
    report = _report_args(technical_analysis="x" * 9000)
    comparison = dedupe._prepare_report_for_comparison(report)
    assert comparison["endpoint"] == "/login"
    assert comparison["method"] == "POST"
    assert comparison["technical_analysis"] == "x" * 8000 + "...[truncated]"
    assert "poc_script_code" not in comparison


@pytest.mark.parametrize(
    "severity,expected", [("Critical", "critical"), (" HIGH ", "high"), ("informational", "info"), ("", None)]
)
async def test_internal_severity_is_canonical(tmp_path, severity, expected):
    ctx = _context(tmp_path)
    arguments = json.dumps(
        {"finding_type": "result", "title": "Fixture", "content": "Synthetic only", "severity": severity}
    )
    tool_ctx = ToolContext.from_agent_context(
        ctx, "internal-fixture", tool_name="create_finding", tool_arguments=arguments
    )
    result = json.loads(await reporting.create_finding.on_invoke_tool(tool_ctx, arguments))
    assert result["success"] is True
    assert ctx.context.run_state.internal_findings[0].get("severity") == expected


async def test_invalid_internal_severity_does_not_save_finding(tmp_path):
    ctx = _context(tmp_path)
    arguments = json.dumps(
        {"finding_type": "result", "title": "Fixture", "content": "Synthetic only", "severity": "urgent"}
    )
    tool_ctx = ToolContext.from_agent_context(
        ctx, "internal-fixture", tool_name="create_finding", tool_arguments=arguments
    )
    result = json.loads(await reporting.create_finding.on_invoke_tool(tool_ctx, arguments))
    assert result["success"] is False
    assert ctx.context.run_state.internal_findings == []
