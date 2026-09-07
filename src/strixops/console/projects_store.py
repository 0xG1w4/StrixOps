"""Project store — user-created groupings above runs.

Projects live in ``~/.strixops/projects.json``.  Run membership is owned by
the console's assignment sidecar because ``run.json`` is rewritten by the
engine. Deleting a project never deletes runs — they become "unassigned".
This module owns CRUD, scope metadata, per-project run statistics aggregation,
and the legacy cross-run report preview.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from strixops.console import parser, project_assignment, project_scope

SCHEMA_VERSION = 2
VALID_COLORS = {"gold", "cyan", "violet", "success", "danger", "neutral"}

# Severity display order for aggregation.
_SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]


def projects_path() -> Path:
    override = (os.environ.get("STRIXOPS_PROJECTS_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".strixops" / "projects.json"


def load_projects() -> dict[str, Any]:
    path = projects_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    projects = data.get("projects")
    if not isinstance(projects, list):
        projects = []
    migrated: list[dict[str, Any]] = []
    for raw in projects:
        if not isinstance(raw, dict):
            continue
        project = dict(raw)
        # Only a genuinely missing v1 field receives the permissive default.
        # An explicitly malformed v2 policy is preserved and later fails
        # closed instead of being silently converted into '*'.
        if "scope" not in project:
            project["scope"] = project_scope.default_scope()
        revision = project.get("scope_revision", 1)
        project["scope_revision"] = revision if type(revision) is int and revision > 0 else 1
        migrated.append(project)
    return {
        "schema_version": SCHEMA_VERSION,
        "projects": migrated,
    }


def save_projects(data: dict[str, Any]) -> None:
    path = projects_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def sanitize_project(
    payload: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    existing = existing or {}
    errors: list[str] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    name = str(payload.get("name") or existing.get("name") or "").strip()
    if not name:
        errors.append("project name is required")
    elif len(name) > 80:
        errors.append("project name too long (max 80 chars)")

    description_value = (
        payload.get("description") if "description" in payload else existing.get("description")
    )
    description = str(description_value or "").strip()
    color = str(payload.get("color") or existing.get("color") or "gold").strip().lower()
    if color not in VALID_COLORS:
        color = "gold"

    raw_scope: object
    if "scope" in payload:
        raw_scope = payload["scope"]
    elif "scope_rules" in payload:
        rules = payload["scope_rules"]
        mode = "unrestricted" if _is_any_rules(rules) else "restricted"
        raw_scope = {"schema_version": 1, "mode": mode, "entries": rules}
    else:
        raw_scope = existing.get("scope", project_scope.default_scope())
    try:
        scope = project_scope.normalize_scope(raw_scope)
    except project_scope.ScopeValidationError as exc:
        errors.extend(exc.errors)
        scope = project_scope.default_scope()

    previous_scope = existing.get("scope")
    previous_revision = existing.get("scope_revision", 1)
    if type(previous_revision) is not int or previous_revision < 1:
        previous_revision = 1
    scope_revision = previous_revision
    if existing and previous_scope is not None:
        try:
            normalized_previous = project_scope.normalize_scope(previous_scope)
        except project_scope.ScopeValidationError:
            normalized_previous = None
        if normalized_previous != scope:
            scope_revision += 1

    project = {
        "id": str(existing.get("id") or f"prj_{secrets.token_hex(4)}"),
        "name": name,
        "description": description,
        "color": color,
        "created_at": str(existing.get("created_at") or now),
        "updated_at": now,
        "scope": scope,
        "scope_revision": scope_revision,
    }
    return project, errors


def public_project(project: dict[str, Any]) -> dict[str, Any]:
    public = {key: value for key, value in project.items() if not str(key).startswith("_")}
    try:
        scope = project_scope.normalize_scope(project.get("scope"))
        scope_valid = True
        scope_error = ""
    except project_scope.ScopeValidationError as exc:
        # Empty restricted rules communicate a fail-closed broken policy to
        # the UI without leaking a permissive fallback into authorization.
        scope = {"schema_version": 1, "mode": "restricted", "entries": []}
        scope_valid = False
        scope_error = str(exc)
    public["scope"] = scope
    public["scope_rules"] = scope["entries"]
    public["scope_mode"] = scope["mode"]
    public["scope_revision"] = int(project.get("scope_revision") or 1)
    public["scope_valid"] = scope_valid
    if scope_error:
        public["scope_error"] = scope_error
    return public


def _is_any_rules(rules: object) -> bool:
    return (
        isinstance(rules, list)
        and len(rules) == 1
        and isinstance(rules[0], dict)
        and rules[0].get("kind") == "any"
        and rules[0].get("value") == "*"
    )


def scope_for_project(project: dict[str, Any]) -> dict[str, Any]:
    """Return the normalized policy; malformed stored policies raise."""
    return project_scope.normalize_scope(project.get("scope"))


def find_project(data: dict[str, Any], project_id: str) -> dict[str, Any] | None:
    for p in data["projects"]:
        if p.get("id") == project_id:
            return p
    return None


# --------------------------------------------------------------- aggregation


def _runs_root() -> Path:
    from strixops.console.server import state

    return state.runs_root


def _run_project_id(run_dir: Path) -> str:
    try:
        return project_assignment.project_id_for_run(run_dir)
    except project_assignment.AssignmentStoreError:
        # A corrupt explicit sidecar must never resurrect a legacy assignment.
        return ""


def _summarize(run_dirs: list[Path]) -> dict[str, Any]:
    """Aggregate stats for a list of run directories."""
    severity: dict[str, int] = {}
    vuln_count = 0
    internal_count = 0
    live = 0
    last_run_at = ""
    llm_usage: dict[str, int] = {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    for run_dir in run_dirs:
        findings = parser.parse_findings(run_dir)
        vuln_count += len(findings["vulnerabilities"])
        internal_count += len(findings["internal"])
        for v in findings["vulnerabilities"]:
            sev = str(v.get("severity") or "info").lower()
            severity[sev] = severity.get(sev, 0) + 1
        record = parser.json_load(run_dir / "run.json")
        start = str(record.get("start_time") or "")
        if start > last_run_at:
            last_run_at = start
        usage = record.get("llm_usage")
        if isinstance(usage, dict):
            for key in llm_usage:
                llm_usage[key] += int(usage.get(key, 0) or 0)
        try:
            events_mtime = (run_dir / "events.jsonl").stat().st_mtime
        except OSError:
            events_mtime = 0
        if record.get("status") == "running" and events_mtime > 0 and (time.time() - events_mtime) < 300:
            live += 1
    return {
        "run_count": len(run_dirs),
        "live_count": live,
        "vulnerability_count": vuln_count,
        "internal_finding_count": internal_count,
        "severity": severity,
        "last_run_at": last_run_at,
        "llm_usage": llm_usage,
    }


def project_summaries() -> dict[str, Any]:
    """All projects with their aggregated run stats, plus unassigned count."""
    data = load_projects()
    root = _runs_root()
    all_run_dirs = []
    if root.is_dir():
        for path in root.iterdir():
            if (
                path.is_dir()
                and not path.name.startswith(".")
                and ((path / "run.json").exists() or (path / "events.jsonl").exists())
            ):
                all_run_dirs.append(path)

    by_project: dict[str, list[Path]] = {p["id"]: [] for p in data["projects"]}
    unassigned: list[Path] = []
    for run_dir in all_run_dirs:
        pid = _run_project_id(run_dir)
        if pid and pid in by_project:
            by_project[pid].append(run_dir)
        else:
            unassigned.append(run_dir)

    projects_out = []
    for p in data["projects"]:
        stats = _summarize(by_project.get(p["id"], []))
        projects_out.append({**public_project(p), **stats})

    projects_out.sort(key=lambda x: x.get("last_run_at") or x.get("created_at") or "", reverse=True)
    return {"projects": projects_out, "unassigned_count": len(unassigned)}


def project_runs(project_id: str) -> list[Path]:
    root = _runs_root()
    if not root.is_dir():
        return []
    dirs = []
    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        if not ((path / "run.json").exists() or (path / "events.jsonl").exists()):
            continue
        if _run_project_id(path) == project_id:
            dirs.append(path)
    return dirs


def project_findings(project_id: str) -> dict[str, Any]:
    """Cross-run aggregated findings for a project."""
    vulnerabilities: list[dict[str, Any]] = []
    internal: list[dict[str, Any]] = []
    by_run: dict[str, dict[str, int]] = {}
    for run_dir in project_runs(project_id):
        findings = parser.parse_findings(run_dir)
        vuln_count = len(findings["vulnerabilities"])
        int_count = len(findings["internal"])
        if vuln_count or int_count:
            by_run[run_dir.name] = {"vulns": vuln_count, "internal": int_count}
        for v in findings["vulnerabilities"]:
            v = dict(v)
            v["source_run"] = run_dir.name
            vulnerabilities.append(v)
        for f in findings["internal"]:
            f = dict(f)
            f["source_run"] = run_dir.name
            internal.append(f)

    vuln_order = {s: i for i, s in enumerate(_SEVERITY_ORDER)}
    vulnerabilities.sort(
        key=lambda v: (
            vuln_order.get(str(v.get("severity") or "info").lower(), 99),
            str(v.get("timestamp") or ""),
        )
    )
    return {"vulnerabilities": vulnerabilities, "internal": internal, "by_run": by_run}


def project_report_markdown(project: dict[str, Any]) -> str:
    """Compose a consolidated project report across all runs."""
    findings = project_findings(project["id"])
    stats = _summarize(project_runs(project["id"]))
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    lines: list[str] = [
        f"# 專案報告 - {project['name']}",
        f"專案描述：{project.get('description') or '—'}",
        f"報告產生時間：{now}",
        f"掃描次數：{stats['run_count']}",
        f"發現總數：{stats['vulnerability_count']} vulns · {stats['internal_finding_count']} internal",
        "---",
        "",
    ]

    if findings["vulnerabilities"]:
        lines += ["## 漏洞清單", ""]
        for v in findings["vulnerabilities"]:
            sev = str(v.get("severity") or "info").upper()
            source = v.get("source_run", "?")
            lines.append(f"- **[{sev}]** {v.get('title', '?')} — 來源: `{source}`")
        lines.append("")

    if findings["internal"]:
        lines += ["## 內網發現", ""]
        for f in findings["internal"]:
            source = f.get("source_run", "?")
            lines.append(f"- **[{f.get('finding_type', '?')}]** {f.get('title', '?')} — 來源: `{source}`")
        lines.append("")

    lines += ["## 各掃描統計", ""]
    for run_name, counts in sorted(findings["by_run"].items()):
        lines.append(f"- `{run_name}`: {counts['vulns']} vulns, {counts['internal']} internal")

    return "\n".join(lines)


def assign_run(run_dir: Path, project_id: str) -> bool:
    """Set or clear authoritative project membership for a run."""
    if not run_dir.is_dir():
        return False
    try:
        if project_id:
            data = load_projects()
            if find_project(data, project_id) is None:
                return False
            project_assignment.write_assignment(run_dir, project_id, source="console-assign")
        else:
            project_assignment.clear_assignment(run_dir, source="console-clear")
        return True
    except project_assignment.AssignmentStoreError:
        return False
