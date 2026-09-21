"""Per-agent request measurements survive persistence and safe API projection."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from strixops.console.server import _build_summary
from strixops.platform.events import EventWriter
from strixops.report.state import RunState


def measurement(tokens=42, **updates):
    return {
        "agent_name": "Root", "model": "route-model", "input_tokens": tokens,
        "source": "estimate", "phase": "request", "updated_at": "2026-09-21T00:00:00+00:00",
        **updates,
    }


def test_request_usage_replaces_one_agent_without_changing_other_agents_or_spend(tmp_path):
    state = RunState(tmp_path, EventWriter(tmp_path))
    state.record_usage("", {"requests": 9, "input_tokens": 123_456, "output_tokens": 10})
    state.record_context_usage("root", measurement(40_000))
    state.record_context_usage("child", measurement(4_000, agent_name="Child"))
    state.record_context_usage("root", measurement(5_000))  # e.g. the first request after compaction
    state.record_context_usage("root", measurement(
        5_120, source="provider_usage", phase="response", prompt="never-store-this",
        api_key="never-store-this-either",
    ))
    saved = json.loads((tmp_path / "run.json").read_text())
    assert saved["agent_context"]["root"] == measurement(5_120, source="provider_usage", phase="response")
    assert saved["agent_context"]["child"]["input_tokens"] == 4_000
    assert saved["llm_usage"]["input_tokens"] == 123_456
    assert "never-store" not in (tmp_path / "run.json").read_text()
    event_text = (tmp_path / "events.jsonl").read_text()
    assert "never-store" not in event_text
    event = json.loads(event_text.splitlines()[-1])
    assert event["event_type"] == "context.usage.updated" and event["actor"]["agent_id"] == "root"
    assert event["payload"]["input_tokens"] == 5_120
    assert _build_summary(tmp_path)["agent_context"] == saved["agent_context"]


def test_concurrent_agent_updates_and_usage_writes_preserve_all_measurements(tmp_path):
    state = RunState(tmp_path, EventWriter(tmp_path))

    def update(index):
        for iteration in range(4):
            state.record_context_usage(f"agent-{index}", measurement(index * 10 + iteration))
            state.record_usage("", {"requests": index, "input_tokens": iteration, "output_tokens": 0})
            state.save()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update, range(4)))
    saved = json.loads((tmp_path / "run.json").read_text())
    assert {key: row["input_tokens"] for key, row in saved["agent_context"].items()} == {
        f"agent-{index}": index * 10 + 3 for index in range(4)
    }


@pytest.mark.parametrize("updates", [
    {"input_tokens": True}, {"input_tokens": -1}, {"input_tokens": 1.5},
    {"source": {}}, {"phase": "unknown"}, {"model": None},
])
def test_invalid_measurements_cannot_overwrite_last_known_snapshot(tmp_path, updates):
    state = RunState(tmp_path, EventWriter(tmp_path))
    state.record_context_usage("root", measurement())
    state.record_context_usage("root", measurement(**updates))
    assert state.run_record["agent_context"]["root"] == measurement()


def test_summary_ignores_malformed_agents_and_only_exposes_safe_scalar_fields(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({
        "agent_context": {
            "root": {**measurement(0), "raw_request": {"secret": "must-not-publish"}},
            "child": measurement(150_000, agent_name="Child", source="provider_usage", phase="response"),
            "bad": measurement(True), "empty": {}, "bad-source": measurement(source=[]),
        },
    }))
    assert _build_summary(tmp_path)["agent_context"] == {
        "root": measurement(0),
        "child": measurement(150_000, agent_name="Child", source="provider_usage", phase="response"),
    }


@pytest.mark.parametrize("saved", [None, {}, {"root": {}}, []])
def test_unknown_context_has_no_fabricated_zero_measurement(tmp_path, saved):
    (tmp_path / "run.json").write_text(json.dumps({"agent_context": saved}))
    assert "agent_context" not in _build_summary(tmp_path)


def test_probe_projection_does_not_expose_raw_diagnostics_or_replace_capacity_with_lower_bound(tmp_path):
    capacity = {
        "model": "route-model", "capacity_tokens": 32_768, "output_limit_tokens": 4_096,
        "capacity_source": "provider_error", "output_source": "configured_fallback",
        "lookup_status": "no_metadata", "compact_trigger_tokens": 16_384,
        "auto_compact": True, "resolved_at": "2026-09-21T00:00:00+00:00",
    }
    probe = {
        "status": "limit_reported", "requests": 2, "largest_accepted_input_tokens": 8_100,
        "smallest_rejected_input_tokens": 40_000, "output_budget_tokens": 64,
        "planned_input_tokens": 48_100,
    }
    (tmp_path / "run.json").write_text(json.dumps({
        "model_context": {**capacity, "probe": {**probe, "raw_error": "secret"}},
    }))
    assert _build_summary(tmp_path)["model_context"] == {**capacity, "probe": probe}


def test_invalid_probe_is_omitted_without_hiding_valid_capacity(tmp_path):
    capacity = {
        "model": "route-model", "capacity_tokens": 32_768, "output_limit_tokens": 4_096,
        "capacity_source": "model_catalog", "output_source": "model_catalog",
        "lookup_status": "no_metadata", "compact_trigger_tokens": 16_384,
        "auto_compact": True, "resolved_at": "2026-09-21T00:00:00+00:00",
    }
    (tmp_path / "run.json").write_text(json.dumps({
        "model_context": {**capacity, "probe": {"status": {}, "raw_error": "secret"}},
    }))
    assert _build_summary(tmp_path)["model_context"] == capacity
