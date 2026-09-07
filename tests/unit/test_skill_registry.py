"""Skill discovery must identify exactly the same safe files as skill loading."""

from __future__ import annotations

import json

import pytest
from agents.tool_context import ToolContext

from strixops import skills as registry
from strixops.agents.prompts import child_instructions, root_instructions
from strixops.engine.scanconfig import EngineContext, ScanSpec
from strixops.tools.skills import list_skills, load_skill


def add_skill(root, canonical_id, *, name="alias", body="SKILL BODY", extra=""):
    path = root / f"{canonical_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Description\n{extra}---\n\n{body}")
    return path


@pytest.fixture
def corpus(monkeypatch, tmp_path):
    root = tmp_path / "content"
    root.mkdir()
    monkeypatch.setattr(registry, "CONTENT_ROOT", root)
    return root


def test_canonical_and_unique_filename_frontmatter_aliases_resolve(corpus):
    path = add_skill(
        corpus, "internal/methodology", name="internal_methodology", extra="aliases: [thinking]\n"
    )
    assert registry.available_skills() == {"internal/methodology": "Description"}
    for name in ["internal/methodology", "methodology", "internal_methodology", "thinking"]:
        assert registry.resolve_skill_path(name) == path
        assert registry.canonical_skill_id(name) == "internal/methodology"


def test_ambiguous_filename_and_frontmatter_names_fail_with_canonical_choices(corpus):
    first = add_skill(corpus, "internal/shared")
    add_skill(corpus, "web/shared")
    assert registry.resolve_skill_path("internal/shared") == first
    for name in ["shared", "alias"]:
        with pytest.raises(registry.SkillResolutionError, match="Ambiguous.*internal/shared, web/shared"):
            registry.resolve_skill_path(name)


@pytest.mark.parametrize(
    "name", ["../outside", "internal/../../outside", "/tmp/outside", "internal//x", "*", "a\\b"]
)
def test_skill_names_cannot_escape_or_glob_the_registry(corpus, name):
    with pytest.raises(registry.SkillResolutionError, match="Invalid skill ID"):
        registry.resolve_skill_path(name)


def test_external_symlink_is_neither_discoverable_nor_loadable(corpus):
    outside = corpus.parent / "outside.md"
    outside.write_text("PRIVATE DATA")
    (corpus / "leak.md").symlink_to(outside)
    assert registry.available_skills() == {}
    assert registry.load_skill_markdown("leak") is None


def test_large_skills_retain_their_final_rules(corpus):
    add_skill(corpus, "internal/long", body="x" * 25_000 + "\nMANDATORY LAST RULE")
    assert registry.load_skill_markdown("internal/long").endswith("MANDATORY LAST RULE")


def test_required_internal_skill_missing_fails_in_root_and_child(corpus):
    spec = ScanSpec(target="10.0.0.0/24", scan_type="internal")
    with pytest.raises(registry.SkillResolutionError, match="internal/core_contract"):
        root_instructions(spec)
    with pytest.raises(registry.SkillResolutionError, match="internal/core_contract"):
        child_instructions("Inspect", spec)


async def test_list_tool_exposes_only_loadable_ids_and_descriptions(corpus):
    add_skill(corpus, "internal/methodology", name="internal_methodology")
    add_skill(corpus, "web/shared", name="other", extra="private_metadata: DO_NOT_EXPOSE\n")
    context = ToolContext(
        context=EngineContext(), tool_name="load_skill", tool_call_id="skills-test", tool_arguments="{}"
    )
    payload = json.loads(await list_skills.on_invoke_tool(context, "{}"))
    assert payload["success"] is True
    assert {row["id"] for row in payload["skills"]} == {"internal/methodology", "web/shared"}
    assert all(set(row) == {"id", "description"} for row in payload["skills"])
    assert str(corpus) not in json.dumps(payload)
    assert "DO_NOT_EXPOSE" not in json.dumps(payload)
    filtered = json.loads(await list_skills.on_invoke_tool(context, '{"category":"internal"}'))
    assert [row["id"] for row in filtered["skills"]] == ["internal/methodology"]
    for row in payload["skills"]:
        result = await load_skill.on_invoke_tool(context, json.dumps({"skills": [row["id"]]}))
        assert f"===== SKILL: {row['id']}" in result


async def test_load_tool_reports_explicit_errors_and_does_not_partially_succeed(corpus):
    add_skill(corpus, "internal/shared")
    add_skill(corpus, "web/shared")
    context = ToolContext(
        context=EngineContext(), tool_name="load_skill", tool_call_id="skills-test", tool_arguments="{}"
    )
    for names, expected in [
        (["internal/shared", "missing"], "Unknown or unavailable"),
        (["shared"], "Ambiguous"),
        (["../outside"], "Invalid skill ID"),
        (["internal/shared"] * 6, "between 1 and 5"),
        ([], "between 1 and 5"),
    ]:
        result = json.loads(await load_skill.on_invoke_tool(context, json.dumps({"skills": names})))
        assert result["success"] is False
        assert expected in result["errors"][0]
        assert not result.get("loaded")


async def test_load_tool_deduplicates_aliases_by_canonical_id(corpus):
    add_skill(corpus, "internal/methodology", name="internal_methodology")
    context = ToolContext(
        context=EngineContext(), tool_name="load_skill", tool_call_id="skills-test", tool_arguments="{}"
    )
    result = await load_skill.on_invoke_tool(
        context, json.dumps({"skills": ["internal/methodology", "internal_methodology", "methodology"]})
    )
    assert result.count("===== SKILL: internal/methodology") == 1
