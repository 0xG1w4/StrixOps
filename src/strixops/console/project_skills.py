"""Project-scoped skill usage analytics.

The console's event stream does not include tool names, so skill events are
identified by their argument shape:

* ``load_skill`` emits ``tool.execution.started`` with only ``skills``.
* ``create_agent`` emits ``tool.execution.started`` with ``name``, ``task``
  and ``skills``, followed by an ``agent.created`` event containing the same
  pre-injected skills.

Counting both sides of ``create_agent`` doubles every injected skill.  This
module intentionally ignores the dispatch-side tool event and counts the
authoritative ``agent.created`` event instead.  Repeated calls to
``load_skill`` remain separate hits because they are real, separate loads.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from strixops.console import parser


def _skill_names(value: Any) -> list[str]:
    """Return unique, normalized skill names from one event, preserving order."""
    if not isinstance(value, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _unique_run_dirs(run_dirs: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw in run_dirs:
        path = Path(raw)
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path.absolute())
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _run_skill_hits(run_dir: Path) -> tuple[dict[str, int], str, dict[str, Any]]:
    """Return ``(skill hits, first event timestamp, configured scope)``."""
    hits: dict[str, int] = {}
    first_timestamp = ""
    event_config: dict[str, Any] = {}
    # ``agent.created`` is contractually unique per agent.  Guard against a
    # replayed duplicate anyway; counting it twice is never meaningful.
    injected_seen: set[tuple[str, str]] = set()

    try:
        lines = (run_dir / "events.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return hits, first_timestamp, event_config

    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        timestamp = str(event.get("timestamp") or "")
        if timestamp and not first_timestamp:
            first_timestamp = timestamp

        event_type = event.get("event_type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        if event_type == "run.configured":
            scan_config = payload.get("scan_config")
            if isinstance(scan_config, dict):
                event_config = scan_config
            continue

        if event_type == "tool.execution.started":
            args = payload.get("args")
            if not isinstance(args, dict):
                continue
            skills = _skill_names(args.get("skills"))
            if not skills:
                continue
            # The dispatch is followed by agent.created with the same injected
            # skills.  The latter proves that the child was actually created.
            if "name" in args and "task" in args:
                continue
            for skill in skills:
                hits[skill] = hits.get(skill, 0) + 1
            continue

        if event_type == "agent.created":
            actor = event.get("actor")
            if not isinstance(actor, dict):
                actor = {}
            actor_key = str(actor.get("agent_id") or actor.get("agent_name") or "")
            for skill in _skill_names(payload.get("skills")):
                dedupe_key = (actor_key, skill)
                if dedupe_key in injected_seen:
                    continue
                injected_seen.add(dedupe_key)
                hits[skill] = hits.get(skill, 0) + 1

    return hits, first_timestamp, event_config


def aggregate_project_skill_analytics(
    run_dirs: Iterable[Path],
    *,
    top_limit: int = 8,
) -> dict[str, Any]:
    """Aggregate skill hits for the supplied project run directories.

    ``run_count`` includes runs with no skill events.  ``runs_with_skills``
    distinguishes actual coverage.  The result intentionally exposes aliases
    used by the existing global analytics UI (``loads``, ``skills``, ``runs``)
    so a project endpoint can be wired without a second transformation.
    """
    dirs = _unique_run_dirs(run_dirs)
    per_skill: dict[str, dict[str, Any]] = {}
    by_run: list[dict[str, Any]] = []

    for run_dir in dirs:
        run_hits, event_start, event_config = _run_skill_hits(run_dir)
        record = parser.json_load(run_dir / "run.json")
        scan_config = record.get("scan_config")
        if not isinstance(scan_config, dict):
            scan_config = {}
        # A recorded full scope must not be replaced by an older event's primary.
        scope = scan_config if "targets" in scan_config else event_config or scan_config
        target_error = ""
        try:
            targets = parser.scan_targets(scope)
        except ValueError:
            targets = []
            target_error = "Recorded target scope is invalid; the complete scope is unknown."

        run_entry = {
            "run": run_dir.name,
            "target": targets[0] if targets else str(scope.get("target") or ""),
            "targets": targets,
            "target_count": len(targets),
            "status": str(record.get("status") or "unknown"),
            "start_time": str(record.get("start_time") or event_start),
            "project_id": str(scan_config.get("project_id") or ""),
            "skills": dict(sorted(run_hits.items())),
            "hits": sum(run_hits.values()),
            # Existing Insights UI calls this field ``total``.
            "total": sum(run_hits.values()),
        }
        if target_error:
            run_entry["target_error"] = target_error
        by_run.append(run_entry)

        for skill, count in run_hits.items():
            entry = per_skill.setdefault(
                skill,
                {
                    "skill": skill,
                    "category": skill.split("/", 1)[0] if "/" in skill else "other",
                    "hits": 0,
                    "loads": 0,
                    "runs": 0,
                },
            )
            entry["hits"] += count
            entry["loads"] += count
            entry["runs"] += 1

    skills = sorted(
        per_skill.values(),
        key=lambda item: (-int(item["hits"]), str(item["skill"])),
    )
    by_run.sort(key=lambda item: (str(item["start_time"]), str(item["run"])), reverse=True)

    categories: dict[str, int] = {}
    for entry in skills:
        category = str(entry["category"])
        categories[category] = categories.get(category, 0) + int(entry["hits"])
    category_rows = sorted(
        ({"category": category, "hits": hits, "loads": hits} for category, hits in categories.items()),
        key=lambda item: (-int(item["hits"]), str(item["category"])),
    )

    total_hits = sum(int(item["hits"]) for item in skills)
    runs_with_skills = sum(1 for item in by_run if int(item["hits"]) > 0)
    limit = max(0, int(top_limit))
    totals = {
        "total_hits": total_hits,
        # Backward-compatible name used by the current Insights page.
        "total_loads": total_hits,
        "distinct_skills": len(skills),
        "run_count": len(by_run),
        # Project workspace calls this out separately from coverage.
        "project_runs": len(by_run),
        "runs_with_skills": runs_with_skills,
    }
    return {
        "totals": totals,
        "top_skills": skills[:limit],
        "skills": skills,
        "categories": category_rows,
        "by_run": by_run,
        # Backward-compatible name used by the current Insights page.
        "runs": by_run,
    }
