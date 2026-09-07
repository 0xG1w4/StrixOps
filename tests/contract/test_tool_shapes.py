"""Every tool's argument schema must classify as its intended kind under the
platform's frozen classifier.

This is the guard against the silent-reclassification trap: adding e.g.
``parent_id`` or ``depth`` to a tool's params makes the platform render it
as a proxy event and garbles the conversation view.
"""

from __future__ import annotations

import pytest

from strixops.agents.factory import base_tools, root_tools
from strixops.tools.lifecycle import agent_finish, finish_scan

from .oracles import infer_unified_tool_context


def _tool_by_name():
    tools = {tool.name: tool for tool in base_tools()}
    tools.update({tool.name: tool for tool in root_tools()})
    tools[finish_scan.name] = finish_scan
    tools[agent_finish.name] = agent_finish
    return tools


EXPECTED_KINDS = {
    "think": "thinking",  # 'thought' key, empty classification → rendered as thinking text
    "create_todo": "todo",
    "update_todo": "todo",
    "load_skill": "generic",
    "list_skills": "generic",
    "record_internal_event": "generic",
    "get_internal_campaign": "generic",
    "web_search": "generic",
    "list_requests": "proxy",
    "view_request": "proxy",
    "repeat_request": "proxy",
    "list_sitemap": "proxy",
    "view_sitemap_entry": "proxy",
    "scope_rules": "proxy",
    "create_vulnerability_report": "vulnerability_report",
    "create_finding": "generic",
    "create_agent": "generic",  # dispatch rendering happens in the deeper parser (task+name+skills)
    "wait_for_agents": "generic",
    "send_message_to_agent": "generic",
    "view_agent_graph": "generic",
    "stop_agent": "generic",
    "finish_scan": "finish",
    "agent_finish": "agent_finish",
}


def _schema_keys(tool) -> set[str]:
    schema = tool.params_json_schema
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    return set(props.keys())


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_KINDS))
def test_tool_classifies_as_intended(tool_name: str) -> None:
    tools = _tool_by_name()
    assert tool_name in tools, f"tool {tool_name} not registered"
    keys = _schema_keys(tools[tool_name])
    # zero-arg tools (e.g. view_agent_graph) legitimately classify as generic
    # the platform classifier always receives a dict of args — feed the schema
    # keys as a representative arg dict (values irrelevant to classification)
    context = infer_unified_tool_context({key: "x" for key in keys})
    expected = EXPECTED_KINDS[tool_name]
    actual = context.get("kind") if context else ("thinking" if "thought" in keys else "generic")
    assert actual == expected, (
        f"{tool_name} args {sorted(keys)} classify as {actual!r}, expected {expected!r}"
    )


def test_finish_scan_has_classifier_four() -> None:
    keys = _schema_keys(finish_scan)
    # the classifier's finish kind requires ALL of these four to be present
    assert {
        "executive_summary",
        "methodology",
        "technical_analysis",
        "recommendations",
    } <= keys
    # optional rich report fields (platform-format sections)
    assert {
        "overall_severity",
        "severity_rationale",
        "battle_gains",
        "attack_narrative",
        "environment_map",
        "credential_capabilities",
        "future_leverage",
        "business_impact",
        "limitations",
    } <= keys


CLASSIFIER_KEYWORDS = {
    "command",
    "cmd",
    "chars",
    "session_id",
    "todos",
    "todo_ids",
    "updates",
    "status",
    "priority",
    "executive_summary",
    "methodology",
    "technical_analysis",
    "recommendations",
    "title",
    "description",
    "impact",
    "endpoint",
    "target",
    "result_summary",
    "httpql_filter",
    "request_id",
    "part",
    "search_pattern",
    "modifications",
    "entry_id",
    "scope_id",
    "allowlist",
    "denylist",
    "parent_id",
    "depth",
}


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_KINDS))
def test_no_unintended_classifier_keywords(tool_name: str) -> None:
    """Any classifier keyword in a tool must be deliberate (covered above)."""
    tools = _tool_by_name()
    keys = _schema_keys(tools[tool_name])
    intended = {k for k in keys if k in CLASSIFIER_KEYWORDS}  # noqa: C401
    # documented, deliberate occurrences only:
    deliberate = {
        "think": set(),  # 'thought' is not a classifier key (rendered as thinking elsewhere)
        "create_todo": {"todos"},
        "update_todo": {"updates"},
        "create_vulnerability_report": {
            "title",
            "description",
            "impact",
            "target",
            "technical_analysis",
            "endpoint",
        },
        "create_finding": {"title"},  # title alone is safe: no description/impact/… companion key
        "create_agent": set(),  # name/task feed the deeper dispatch renderer, not the classifier
        "wait_for_agents": set(),
        "send_message_to_agent": {"priority"},  # present but never alone → subset rule not triggered
        "view_agent_graph": set(),
        "stop_agent": set(),
        "load_skill": set(),
        "list_skills": set(),
        "record_internal_event": set(),
        "get_internal_campaign": set(),
        "web_search": set(),  # query does not participate in the platform classifier
        "list_requests": {"httpql_filter", "scope_id"},
        "view_request": {"request_id", "part", "search_pattern"},
        "repeat_request": {"request_id", "modifications"},
        "list_sitemap": {"scope_id", "parent_id", "depth"},
        "view_sitemap_entry": {"entry_id"},
        "scope_rules": {"allowlist", "denylist", "scope_id"},
        "finish_scan": {"executive_summary", "methodology", "technical_analysis", "recommendations"},  # noqa: E501
        "agent_finish": {"result_summary"},
    }
    assert intended == deliberate[tool_name], (
        f"{tool_name} carries classifier keywords beyond the deliberate set: "
        f"{intended ^ deliberate[tool_name]}"
    )
