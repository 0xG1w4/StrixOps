"""Default skill references must exist in the corpus.

Regression: ``tooling/shell`` was referenced by ``default_root_skills`` but the
corpus has tool-specific files instead — the skill silently never loaded.
"""

from __future__ import annotations

import pytest

from strixops import skills as skill_registry


@pytest.mark.parametrize("scan_type", ["web", "internal"])
def test_default_root_skills_all_resolve(scan_type: str) -> None:
    names = skill_registry.default_root_skills(scan_type)
    assert names, "default skill set must not be empty"
    for name in names:
        assert skill_registry.resolve_skill_path(name) is not None, f"missing skill: {name}"
        assert skill_registry.load_skill_markdown(name), f"empty skill content: {name}"


def test_available_skills_nonempty_and_named() -> None:
    available = skill_registry.available_skills()
    assert len(available) >= 60  # ported corpus
    assert all(isinstance(name, str) and name for name in available)
    for canonical_id in available:
        assert "/" in canonical_id
        assert skill_registry.canonical_skill_id(canonical_id) == canonical_id
