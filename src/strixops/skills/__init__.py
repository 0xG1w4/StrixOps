"""Skill corpus registry — markdown discipline files loaded by name.

The corpus lives under ``content/<category>/<name>.md`` with YAML frontmatter
(``name``, ``description``). Skill names are stable identifiers: prompts and
agents reference them by name (``vulnerabilities/sql_injection``), so renaming
a file breaks references — treat names as API.

Corpus provenance: ported in substance from the Strix project (Apache-2.0);
see NOTICE.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import yaml

CONTENT_ROOT = Path(__file__).parent / "content"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class SkillResolutionError(ValueError):
    """A requested skill cannot be selected unambiguously and safely."""


@dataclass(frozen=True)
class _Skill:
    canonical_id: str
    description: str
    path: Path
    aliases: frozenset[str]
    content: str


_FROZEN: ContextVar[tuple[_Skill, ...] | None] = ContextVar("strixops_skill_snapshot", default=None)


def _parse_frontmatter(text: str) -> dict:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _valid_name(name: str) -> bool:
    return bool(
        name
        and all(part not in ("", ".", "..") for part in name.split("/"))
        and re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", name)
    )


def _entries() -> list[_Skill]:
    """Private filesystem inventory; tools expose only IDs and descriptions."""
    frozen = _FROZEN.get()
    if frozen is not None:
        return list(frozen)
    root = CONTENT_ROOT.resolve()
    entries: list[_Skill] = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        canonical_id = path.relative_to(root).with_suffix("").as_posix()
        if not _valid_name(canonical_id):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue
        meta = _parse_frontmatter(text)
        declared_aliases = meta.get("aliases", [])
        if isinstance(declared_aliases, str):
            declared_aliases = [declared_aliases]
        if not isinstance(declared_aliases, list):
            declared_aliases = []
        aliases = frozenset(
            alias.strip()
            for alias in [path.stem, meta.get("name", ""), *declared_aliases]
            if isinstance(alias, str) and _valid_name(alias.strip())
        )
        description = meta.get("description", "")
        entries.append(
            _Skill(canonical_id, description if isinstance(description, str) else "", path, aliases, text)
        )
    return entries


def available_skills() -> dict[str, str]:
    """Loadable canonical ID → description, without private registry fields."""
    return {entry.canonical_id: entry.description for entry in _entries()}


def _resolve(name: str) -> _Skill | None:
    clean = name.strip()
    if not _valid_name(clean):
        raise SkillResolutionError(f"Invalid skill ID: {name!r}. Use list_skills for canonical IDs.")
    entries = _entries()
    # An exact canonical ID takes precedence over every shorthand alias.
    for entry in entries:
        if entry.canonical_id == clean:
            return entry
    matches = [entry for entry in entries if clean in entry.aliases]
    if len(matches) > 1:
        choices = ", ".join(entry.canonical_id for entry in matches)
        raise SkillResolutionError(f"Ambiguous skill {clean!r}; choose a canonical ID: {choices}")
    return matches[0] if matches else None


def canonical_skill_id(name: str) -> str:
    entry = _resolve(name)
    if entry is None:
        raise SkillResolutionError(f"Unknown or unavailable skill: {name!r}. Use list_skills.")
    return entry.canonical_id


def resolve_skill_path(name: str) -> Path | None:
    """Resolve a canonical ID or unique filename/frontmatter alias.

    Missing names return None; invalid paths and ambiguous aliases explicitly
    fail instead of selecting an arbitrary file.
    """
    entry = _resolve(name)
    return entry.path if entry is not None else None


def load_skill_markdown(name: str) -> str | None:
    entry = _resolve(name)
    return entry.content if entry is not None else None


def snapshot_skills() -> list[dict]:
    """Capture all lazy-loadable text and aliases for one run."""
    return [
        {
            "id": entry.canonical_id,
            "description": entry.description,
            "aliases": sorted(entry.aliases),
            "content": entry.content,
        }
        for entry in _entries()
    ]


@contextmanager
def use_skill_snapshot(records: list[dict]):
    entries = tuple(
        _Skill(
            row["id"],
            row["description"],
            CONTENT_ROOT / (row["id"] + ".md"),
            frozenset(row["aliases"]),
            row["content"],
        )
        for row in records
    )
    token = _FROZEN.set(entries)
    try:
        yield
    finally:
        _FROZEN.reset(token)


def default_root_skills(scan_type: str) -> list[str]:
    """Skills pre-loaded for the root agent by scan type.

    Names must exist in the corpus (verified by test). The methodology skill
    is pre-loaded for internal scans as the thinking framework; deeper
    technique skills are loaded by the agent on demand via load_skill.
    """
    if scan_type == "internal":
        return ["internal/core_contract", "internal/methodology"]
    return [
        "tooling/python",
        "tooling/agent_browser",
        "analysis/counterevidence",
        "analysis/severity_calibration",
    ]


def default_child_skills(scan_type: str) -> list[str]:
    """Every web worker receives the same evidence and tooling foundations."""
    return ["internal/core_contract"] if scan_type == "internal" else default_root_skills("web")
