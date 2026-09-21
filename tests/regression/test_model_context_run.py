"""Startup capacity is frozen per run and exposed without provider credentials."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from strixops.config import provider
from strixops.config.context import ContextSettings
from strixops.config.settings import EngineSettings
from strixops.engine import runner
from strixops.engine.model_capacity import ModelCapacity
from strixops.engine.scanconfig import ScanSpec
from strixops.runtime import sandbox as sandbox_module


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("STRIXOPS_QUEUE_DB", str(tmp_path / "queue.sqlite3"))
    monkeypatch.setenv("STRIXOPS_REPORT_SYNTHESIS", "0")
    monkeypatch.delenv("STRIXOPS_QUEUE_ITEM_ID", raising=False)
    monkeypatch.delenv("STRIXOPS_QUEUE_ITEM_TOKEN", raising=False)
    monkeypatch.delenv("STRIXOPS_QUEUE_PROCESS_BOUNDARY", raising=False)
    run_dir = tmp_path / "runs" / "capacity-fixture"
    settings = EngineSettings(
        llm_api_base="https://provider.invalid/v1", llm_api_key="private-fixture-key",
        strix_llm="selected-model", strix_runs=str(run_dir.parent),
        operator_hints_dir="", host_workspace_dir="", dry_run=False,
    )
    capacity = ModelCapacity(
        model="selected-model", capacity_tokens=32_768, output_limit_tokens=4_096,
        capacity_source="provider_metadata", output_source="provider_metadata", lookup_status="resolved",
    )
    monkeypatch.setattr(runner, "generate_run_name", lambda *_: run_dir.name)
    monkeypatch.setattr(runner, "ContextSettings", lambda: ContextSettings(auto_compact=True))
    monkeypatch.setattr(
        provider, "make_platform_model", Mock(return_value=SimpleNamespace(model="selected-model"))
    )
    monkeypatch.setattr(runner, "build_root_agent", Mock(return_value=SimpleNamespace(name="root")))
    sandbox = SimpleNamespace(session=object(), teardown=AsyncMock(), quiesce=AsyncMock())
    create_sandbox = AsyncMock(return_value=sandbox)
    monkeypatch.setattr(sandbox_module, "create_sandbox_session", create_sandbox)
    resolver = AsyncMock(return_value=capacity)
    monkeypatch.setattr(runner, "resolve_model_capacity", resolver)
    probe = AsyncMock(side_effect=lambda _settings, _context, resolved, **_kwargs: resolved)
    monkeypatch.setattr(runner, "probe_model_capacity", probe)
    agent_loop = AsyncMock(return_value=None)
    monkeypatch.setattr(runner, "run_agent_loop", agent_loop)
    return SimpleNamespace(
        settings=settings, capacity=capacity, run_dir=run_dir, resolver=resolver,
        create_sandbox=create_sandbox, agent_loop=agent_loop, probe=probe,
    )


@pytest.mark.asyncio
async def test_capacity_is_saved_before_sandbox_and_passed_to_agent_services(runtime):
    def events():
        return [json.loads(line) for line in (runtime.run_dir / "events.jsonl").read_text().splitlines()]

    async def resolve(settings, context):
        assert events()[0]["event_type"] == "run.configured"
        assert not runtime.create_sandbox.called
        assert settings is runtime.settings
        return runtime.capacity

    async def before_sandbox(**_kwargs):
        saved = json.loads((runtime.run_dir / "run.json").read_text())["model_context"]
        assert saved["capacity_tokens"] == 32_768
        assert saved["capacity_source"] == "provider_metadata"
        assert any(event["event_type"] == "model.context.resolved" for event in events())
        return SimpleNamespace(session=object(), teardown=AsyncMock(), quiesce=AsyncMock())

    runtime.resolver.side_effect = resolve
    runtime.create_sandbox.side_effect = before_sandbox
    # The synthetic loop stops without a finish tool; no model or scan runs.
    assert await runner.run_scan(ScanSpec("https://target.invalid"), runtime.settings) == runner.EXIT_FAILED
    runtime.resolver.assert_awaited_once()
    runtime.probe.assert_awaited_once()
    context = runtime.agent_loop.call_args.args[1]
    assert context.services.model_capacity is runtime.capacity
    saved = json.loads((runtime.run_dir / "run.json").read_text())["model_context"]
    assert saved["compact_trigger_tokens"] < saved["capacity_tokens"]
    assert saved["auto_compact"] is True and saved["resolved_at"]
    capacity_events = [event for event in events() if event["event_type"] == "model.context.resolved"]
    assert len(capacity_events) == 1 and capacity_events[0]["payload"] == saved
    assert runtime.settings.llm_api_key not in json.dumps(saved)


@pytest.mark.asyncio
async def test_invalid_route_stops_before_metadata_lookup(runtime):
    invalid = replace(runtime.settings, llm_api_key="")
    assert await runner.run_scan(ScanSpec("https://target.invalid"), invalid) == runner.EXIT_FAILED
    runtime.resolver.assert_not_awaited()
    runtime.probe.assert_not_awaited()
    runtime.create_sandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_result_controls_saved_budget_and_probe_usage_is_counted(runtime):
    corrected = replace(runtime.capacity, capacity_tokens=8_192, capacity_source="provider_error")

    async def probe(settings, context, capacity, *, on_usage):
        assert capacity is runtime.capacity
        assert settings is runtime.settings
        assert not runtime.create_sandbox.called
        on_usage({"input_tokens": 6_000, "output_tokens": 16, "total_tokens": 6_016})
        return corrected

    runtime.probe.side_effect = probe
    assert await runner.run_scan(ScanSpec("https://target.invalid"), runtime.settings) == runner.EXIT_FAILED
    runtime.probe.assert_awaited_once()
    context = runtime.agent_loop.call_args.args[1]
    assert context.services.model_capacity is corrected
    saved = json.loads((runtime.run_dir / "run.json").read_text())
    assert saved["model_context"]["capacity_tokens"] == 8_192
    assert saved["model_context"]["compact_trigger_tokens"] == 4_096
    assert saved["llm_usage"] == {
        "requests": 1, "input_tokens": 6_000, "output_tokens": 16, "total_tokens": 6_016,
    }


@pytest.mark.asyncio
async def test_dry_run_never_queries_model_metadata(runtime, monkeypatch):
    # A tiny in-memory gateway double keeps this test independent of local
    # ignored dry-run fixtures, containers and network listeners.
    module = ModuleType("strixops.testing.scripted_gateway")
    gateway = SimpleNamespace(
        start=Mock(), stop=Mock(), make_model=Mock(return_value=SimpleNamespace(model="fixture")),
    )
    module.ScriptedGateway = Mock(return_value=gateway)
    for name in (
        "child_script", "hint_scenario_scripts", "internal_script", "web_script", "web_script_with_shell",
    ):
        setattr(module, name, Mock(return_value=[]))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.delenv("STRIXOPS_DRY_RUN_SANDBOX", raising=False)
    monkeypatch.delenv("STRIXOPS_DRY_RUN_HINTS", raising=False)
    settings = replace(runtime.settings, dry_run=True)
    assert await runner.run_scan(ScanSpec("https://target.invalid"), settings) == runner.EXIT_FAILED
    runtime.resolver.assert_not_awaited()
    runtime.probe.assert_not_awaited()
    runtime.create_sandbox.assert_not_awaited()
    assert "model_context" not in json.loads((runtime.run_dir / "run.json").read_text())


@pytest.mark.parametrize("capacity_tokens,trigger", [(32_768, 16_384), (1, 0)])
def test_summary_projects_saved_capacity_and_omits_unrelated_fields(tmp_path, capacity_tokens, trigger):
    from strixops.console.server import _build_summary

    saved = {
        "model": "old-route-model", "capacity_tokens": capacity_tokens, "output_limit_tokens": 4_096,
        "capacity_source": "configured_fallback", "output_source": "configured_fallback",
        "lookup_status": "no_metadata", "compact_trigger_tokens": trigger,
        "auto_compact": False, "resolved_at": "2026-09-21T00:00:00+00:00",
    }
    (tmp_path / "run.json").write_text(json.dumps({
        "scan_config": {"target": "https://target.invalid"},
        "model_context": {**saved, "api_key": "must-not-be-published", "raw_response": {}},
    }))
    assert _build_summary(tmp_path)["model_context"] == saved


@pytest.mark.parametrize("invalid", [None, {}, {"capacity_source": {}}, {"capacity_tokens": True}])
def test_old_or_malformed_runs_have_no_capacity_claim(tmp_path, invalid):
    from strixops.console.server import _build_summary

    (tmp_path / "run.json").write_text(json.dumps({"model_context": invalid}))
    assert "model_context" not in _build_summary(tmp_path)
