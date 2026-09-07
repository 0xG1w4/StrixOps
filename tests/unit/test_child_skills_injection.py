"""create_agent(skills=[...]) must inject skill content into the child prompt.

Regression: the skills parameter used to be recorded (agent registry/events)
without injecting the playbook into the child's system prompt — the child
never saw the discipline the root asked for.
"""

from __future__ import annotations

import pytest

from strixops.agents.factory import build_child_agent, build_root_agent
from strixops.agents.prompts import child_instructions, root_instructions
from strixops.engine.scanconfig import ScanSpec
from strixops.skills import SkillResolutionError, load_skill_markdown


def _spec() -> ScanSpec:
    return ScanSpec(target="https://t.example.com", scan_type="web")


def test_child_agent_prompt_contains_requested_skill():
    agent = build_child_agent(
        "sqli-prober", "probe injection points", skills=["vulnerabilities/sql_injection"]
    )
    content = load_skill_markdown("vulnerabilities/sql_injection")
    assert content, "corpus skill must exist"
    assert content[:400] in str(agent.instructions)


def test_child_agent_without_skills_has_no_skill_block():
    agent = build_child_agent("generic", "look around")
    assert "===== SKILL:" not in str(agent.instructions)


def test_unknown_explicit_skill_fails_before_child_starts():
    with pytest.raises(SkillResolutionError, match="does/not-exist"):
        build_child_agent("x", "task", skills=["does/not-exist", "tooling/python"])


def test_root_prompt_keeps_default_skills():
    agent = build_root_agent(_spec())
    instructions = str(agent.instructions)
    assert "===== SKILL: tooling/python" in instructions
    assert "===== SKILL: analysis/counterevidence" in instructions


@pytest.mark.parametrize("builder", [root_instructions, lambda spec: child_instructions("Review", spec)])
def test_internal_agents_always_receive_scope_constraints_and_access_contract(builder):
    spec = ScanSpec(
        target="10.40.8.0/24",
        scan_type="internal",
        socks5_proxy="127.0.0.1:9080",
        instruction_text="Exclude 10.40.8.10. Do not create accounts.",
    )
    prompt = builder(spec)
    assert "10.40.8.0/24" in prompt
    assert spec.instruction_text in prompt
    assert "socks5_network" in prompt and spec.socks5_proxy in prompt
    assert "does not establish a shell" in prompt
    assert "Initial verified target host: none" in prompt
    assert "Initial verified remote session: none" in prompt
    assert "===== SKILL: internal/core_contract" in prompt
    assert "get_internal_campaign" in prompt and "record_internal_event" in prompt
    assert "successful lifecycle tool call" in prompt


@pytest.mark.parametrize("builder", [root_instructions, lambda spec: child_instructions("Review", spec)])
def test_gsocket_access_is_claimed_until_service_and_session_are_verified(builder):
    spec = ScanSpec(target="10.40.8.12", scan_type="internal", gsocket_key="operator-key")
    prompt = builder(spec)
    assert "gsocket_claimed_shell" in prompt and "operator-key" in prompt
    assert "A remote shell is claimed but not yet verified" in prompt
    assert "Adding -p to a shell connection does not automatically create a SOCKS proxy" in prompt
    assert "-p 1088 &" not in prompt


def test_internal_root_preloads_only_contract_and_methodology():
    prompt = root_instructions(ScanSpec(target="10.40.8.0/24", scan_type="internal"))
    assert "===== SKILL: internal/core_contract" in prompt
    assert "===== SKILL: internal/methodology" in prompt
    assert prompt.count("===== SKILL:") == 2
    assert len(prompt) < 20_000


def test_internal_child_contract_is_not_duplicated_when_explicitly_requested():
    prompt = child_instructions(
        "Review",
        ScanSpec(target="10.40.8.12", scan_type="internal"),
        skills=["internal_core_contract", "tooling/python"],
    )
    assert prompt.count("===== SKILL: internal/core_contract") == 1
    assert "===== SKILL: tooling/python" in prompt


def test_internal_child_reporting_allows_complete_datasets_and_useful_source_paths():
    prompt = child_instructions("Review", ScanSpec(target="10.40.8.12", scan_type="internal"))
    assert "report each distinct discovery promptly" in prompt
    assert "datasets may use complete attachments" in prompt
    assert "never batch" not in prompt
    assert "private implementation paths" in prompt
    assert "Target source paths and relative evidence references are" in prompt
    assert "Never expose internal paths" not in prompt


def test_missing_required_prompt_part_fails_loudly(monkeypatch, tmp_path):
    from strixops.agents import prompts

    monkeypatch.setattr(prompts, "PROMPT_PARTS_DIR", tmp_path)
    with pytest.raises(ValueError, match="Required prompt part"):
        root_instructions(_spec())
