"""Proxy stream events remain recognizable when optional args are omitted."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from strixops.platform.events import EventWriter
from tests.contract.oracles import infer_unified_tool_context


def _emit_call(tmp_path: Path, name: str, args: Any, *, attr_shaped: bool) -> dict[str, Any]:
    raw = {"type": "function_call", "name": name, "call_id": "call-1", "arguments": args}
    item = {"type": "tool_call_item", "raw_item": raw}
    event = {"type": "run_item_stream_event", "name": "tool_called", "item": item}
    if attr_shaped:
        event = SimpleNamespace(
            **{**event, "item": SimpleNamespace(**{**item, "raw_item": SimpleNamespace(**raw)})}
        )
    writer = EventWriter(tmp_path)
    writer.sdk_event("agent-1", "tester", event)
    return json.loads(writer.path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("attr_shaped", [False, True])
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("list_requests", {}),
        ("list_requests", {"first": 10, "after": "cursor-1"}),
        ("view_request", {"request_id": "1"}),
        ("repeat_request", {"request_id": "1"}),
        ("list_sitemap", {}),
        ("list_sitemap", {"page": 2}),
        ("view_sitemap_entry", {"entry_id": "2"}),
        ("scope_rules", {"action": "list"}),
        ("scope_rules", {"action": "create", "scope_name": "target"}),
    ],
)
def test_proxy_calls_classify_without_optional_args(tmp_path, name, args, attr_shaped):
    event = _emit_call(tmp_path, name, json.dumps(args), attr_shaped=attr_shaped)
    assert event["event_type"] == "tool.execution.started"
    assert event["actor"] == {"agent_id": "agent-1", "agent_name": "tester"}
    assert set(event["payload"]) == {"args"}
    assert infer_unified_tool_context(event["payload"]["args"]) == {"kind": "proxy"}


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("list_requests", {"httpql_filter": 'req.method.eq:"POST"', "first": 10}),
        ("list_sitemap", {"scope_id": "scope-1", "parent_id": "entry-1"}),
        ("scope_rules", {"action": "update", "scope_id": "scope-1", "allowlist": ["example.com"]}),
    ],
)
def test_explicit_proxy_args_are_preserved(tmp_path, name, args):
    original = deepcopy(args)
    event = _emit_call(tmp_path, name, args, attr_shaped=False)
    assert event["payload"]["args"] == original
    assert args == original


@pytest.mark.parametrize(
    ("name", "args", "expected_kind"),
    [
        ("exec_command", {"cmd": "pwd"}, "shell"),
        ("list_tasks", {}, None),
        ("web_search", {"query": "Caido HTTPQL"}, None),
        ("custom_scope_rules", {"action": "list"}, None),
    ],
)
def test_non_proxy_calls_are_unchanged(tmp_path, name, args, expected_kind):
    event = _emit_call(tmp_path, name, json.dumps(args), attr_shaped=True)
    assert event["payload"] == {"args": args}
    assert infer_unified_tool_context(event["payload"]["args"]).get("kind") == expected_kind


def test_omitted_defaults_do_not_mutate_source_args(tmp_path):
    args = {"action": "list"}
    event = _emit_call(tmp_path, "scope_rules", args, attr_shaped=False)
    assert event["payload"]["args"] == {"action": "list", "scope_id": None}
    assert args == {"action": "list"}
