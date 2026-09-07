"""Skill loading tool."""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from strixops import skills as skill_registry
from strixops.engine.scanconfig import EngineContext

MAX_SKILLS_PER_CALL = 5


@function_tool(strict_mode=False)
def list_skills(ctx: RunContextWrapper[EngineContext], category: str = "") -> str:
    """Discover canonical IDs and descriptions for available skill playbooks.

    Args:
        category: Optional category such as internal, tooling or vulnerabilities.
            Omit to list every skill. Pass returned IDs to load_skill or create_agent.
    """
    prefix = category.strip().rstrip("/")
    inventory = skill_registry.available_skills()
    entries = [
        {"id": canonical_id, "description": description}
        for canonical_id, description in inventory.items()
        if not prefix or canonical_id.startswith(prefix + "/")
    ]
    return json.dumps({"success": True, "skills": entries}, ensure_ascii=False)


@function_tool(strict_mode=False)
def load_skill(ctx: RunContextWrapper[EngineContext], skills: list[str]) -> str:
    """Load pentest discipline knowledge into your working context.

    Skills are deep playbooks (attack classes, technologies, methodology
    rubrics). Load the relevant ones BEFORE probing a surface — they encode
    what to test, common pitfalls, and evidence standards.

    Args:
        skills: Skill names (up to 5 per call), e.g.
            ["vulnerabilities/sql_injection", "frameworks/django"].
            Discover canonical IDs with list_skills. Unique filename/frontmatter
            aliases also work; invalid, unknown or ambiguous names fail explicitly.
    """
    if not skills or len(skills) > MAX_SKILLS_PER_CALL:
        return json.dumps({"success": False, "errors": ["provide between 1 and 5 skill names"]})
    loaded: set[str] = set()
    errors: list[str] = []
    parts: list[str] = []
    for name in skills:
        try:
            canonical_id = skill_registry.canonical_skill_id(name)
            if canonical_id in loaded:
                continue
            content = skill_registry.load_skill_markdown(canonical_id)
            if not content:
                raise skill_registry.SkillResolutionError(f"Skill is empty or unavailable: {canonical_id}")
            loaded.add(canonical_id)
            parts.append(f"===== SKILL: {canonical_id} =====\n{content}\n")
        except skill_registry.SkillResolutionError as exc:
            errors.append(str(exc))
    if errors:
        return json.dumps({"success": False, "errors": errors, "loaded": []})
    return "\n".join(parts)
