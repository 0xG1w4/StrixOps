"""Events → conversation messages, for the console's live view.

A fresh, compact parser over the engine's own ``events.jsonl`` vocabulary —
the same argument-shape classification the platform's read side applies, so
tool events render with the right kind (shell/todo/finish/report/…).
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from strixops.engine.targets import normalize_targets

_SEVERITIES = {"critical", "high", "medium", "low", "info"}

# Hint-echo tokens accept the full token alphabet the writers produce —
# hex (console token_hex), and mixed alphanumerics like the scripted
# fixture's "echo12345" — so acks never silently miss on charset.
_HINT_ACK_RE = re.compile(r"\[hint:([0-9a-zA-Z_-]+)\]")


def json_load(path: Path) -> dict[str, Any]:
    """Read a JSON object file; {} on any failure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def scan_targets(scan_config: dict[str, Any]) -> list[str]:
    """Read a complete recorded scope without dropping malformed secondary targets.

    Missing legacy targets are unknown, not an empty multi-target launch. A
    present but invalid list raises so assignment and scope checks fail closed.
    """
    if not isinstance(scan_config, dict):
        raise ValueError("scan_config must be an object")
    primary = scan_config.get("target", "")
    if "targets" not in scan_config:
        if primary in ("", None):
            return []
        return normalize_targets(primary)
    values = scan_config["targets"]
    if not isinstance(values, list) or not values:
        raise ValueError("recorded targets must be a nonempty list")
    result = normalize_targets(primary, values)
    if "target_count" in scan_config and (
        type(scan_config["target_count"]) is not int or scan_config["target_count"] != len(result)
    ):
        raise ValueError("recorded target_count does not match targets")
    return result


def infer_tool_kind(args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {}
    if any(k in args for k in ("command", "cmd", "chars", "session_id")):
        cmd = str(args.get("command") or args.get("cmd") or args.get("chars") or "")
        return {"kind": "shell", "command": cmd}
    if any(k in args for k in ("todos", "todo_ids", "updates")):
        action = "created" if "todos" in args else "updated" if "updates" in args else "marked"
        return {"kind": "todo", "todo_action": action}
    if all(k in args for k in ("executive_summary", "methodology", "technical_analysis", "recommendations")):
        return {"kind": "finish"}
    vuln_keys = ("description", "impact", "technical_analysis", "endpoint", "target")
    if "title" in args and any(k in args for k in vuln_keys):
        return {"kind": "vulnerability_report", "title": str(args.get("title") or "")}
    if "result_summary" in args:
        return {"kind": "agent_finish", "result_summary": str(args.get("result_summary") or "")}
    if "name" in args and "task" in args:
        return {"kind": "dispatch", "name": str(args.get("name") or "")}
    if "thought" in args:
        return {"kind": "thinking"}
    if "finding_type" in args and "title" in args:
        return {"kind": "internal_finding", "title": str(args.get("title") or "")}
    return {}


def parse_conversation(events_file: Path, after: int = -1) -> list[dict[str, Any]]:
    """Parse events.jsonl into renderable message records (index > after)."""
    return read_conversation(events_file, after)[0]


def read_event_lines(events_file: Path) -> list[tuple[int, str]]:
    """All non-blank lines as ``(index, raw_line)`` — blanks filtered, then indexed.

    Callers slicing by index must use this list, never raw ``splitlines()``
    output, so indices and counts can never disagree.
    """
    try:
        raw = events_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    pairs: list[tuple[int, str]] = []
    index = -1
    for line in raw:
        if not line.strip():
            continue
        index += 1
        pairs.append((index, line))
    return pairs


def read_conversation(events_file: Path, after: int = -1) -> tuple[list[dict[str, Any]], int]:
    """One-read conversation parse: messages past ``after`` plus the resume cursor.

    The cursor is the index of the last consumed event line — it advances past
    EVERY non-blank line, including lines that map to no message (unknown event
    types, malformed JSON), so a client resuming from it never re-receives tail
    messages. Messages and cursor derive from the same single file read.
    """
    messages: list[dict[str, Any]] = []
    cursor = after
    if not events_file.exists():
        return messages, cursor
    try:
        lines = events_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return messages, cursor
    pending_tool: dict[str, dict[str, Any]] = {}
    index = -1
    for line in lines:
        if not line.strip():
            continue
        index += 1
        if index <= after:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            cursor = index
            continue
        messages.append(_to_message(event, index, pending_tool))
        cursor = index
    return [m for m in messages if m], cursor


def hint_ack_tokens(events_file: Path) -> set[str]:
    """Hint tokens the agents have echoed as ``[hint:TOKEN]`` in chat messages."""
    acked: set[str] = set()
    for _, line in read_event_lines(events_file):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") != "chat.message":
            continue
        content = str((event.get("payload") or {}).get("content") or "")
        acked.update(_HINT_ACK_RE.findall(content))
    return acked


def _to_message(event: dict, index: int, pending_tool: dict) -> dict | None:
    etype = event.get("event_type") or ""
    actor = event.get("actor") or {}
    payload = event.get("payload") or {}
    base = {
        "id": str(index),
        "timestamp": event.get("timestamp") or "",
        "agent_id": actor.get("agent_id") or "",
        "agent_name": actor.get("agent_name") or "",
    }

    if etype == "chat.message":
        return {**base, "type": "message", "content": payload.get("content") or ""}

    if etype == "tool.execution.started":
        args = payload.get("args") or {}
        context = infer_tool_kind(args)
        kind = context.get("kind") or "tool"
        return {
            **base,
            "type": kind,
            "args": args,
            "title": context.get("title") or context.get("name") or context.get("command") or "",
        }

    if etype == "tool.execution.updated":
        return {**base, "type": "output", "result": payload.get("result")}

    if etype == "vulnerability.found":
        finding = payload.get("finding") or {}
        severity = str(finding.get("severity") or "").upper()
        return {
            **base,
            "type": "report",
            "title": finding.get("title") or payload.get("report_id") or "finding",
            "severity": severity,
            "report_id": payload.get("report_id") or "",
        }

    if etype == "finding.internal_created":
        finding = payload.get("finding") or {}
        return {
            **base,
            "type": "internal_finding",
            "title": finding.get("title") or finding.get("id") or "internal finding",
            "severity": str(finding.get("severity") or "").upper(),
            "finding_type": finding.get("finding_type") or "",
        }

    if etype == "agent.created":
        return {**base, "type": "system", "content": f"agent '{actor.get('agent_name')}' created"}
    if etype == "agent.status.updated":
        return {**base, "type": "system", "content": f"status → {payload.get('status')}"}
    if etype == "run.configured":
        return {**base, "type": "system", "content": "run configured"}
    if etype == "run.completed":
        return {
            **base,
            "type": "system",
            "content": f"run completed — {payload.get('vulnerability_count', 0)} finding(s), "
            f"{payload.get('duration_seconds', 0)}s",
        }
    return None


def finite_json_float(value: str) -> float:
    """Reject overflow syntax too, not only the nonstandard NaN/Infinity tokens."""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON number")
    return result


def _displayable_vulnerability(value: Any) -> bool:
    """Keep valid legacy records verbatim; do not coerce corrupt values into facts."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"].strip():
        return False
    text_fields = (
        "title",
        "severity",
        "timestamp",
        "target",
        "description",
        "impact",
        "technical_analysis",
        "poc_description",
        "poc_script_code",
        "remediation_steps",
        "evidence",
        "cvss_vector",
        "endpoint",
        "method",
        "cve",
        "cwe",
        "confidence",
        "confidence_rationale",
        "severity_change_conditions",
        "fix_effort",
        "finding_class",
        "fix_verification",
        "fix_pr_body",
        "updated_at",
        "discovered_by_agent",
        "discovered_by_agent_name",
        "agent_id",
        "agent_name",
        "poc_language",
    )
    if any(value.get(key) is not None and not isinstance(value[key], str) for key in text_fields):
        return False
    score = value.get("cvss")
    if score is not None and (type(score) not in (int, float) or not 0 <= score <= 10):
        return False
    # Older metadata can be a string; the detail renderer safely preserves it
    # and the project aggregator treats non-object metadata as unknown identity.
    for field in ("code_locations", "update_history"):
        items = value.get(field)
        if items is not None and not (
            isinstance(items, list) and all(isinstance(item, dict) for item in items)
        ):
            return False
    return True


def parse_findings(run_dir: Path) -> dict[str, Any]:
    """Vulnerabilities (json) + internal findings (md headers)."""
    vulns: list[dict[str, Any]] = []
    warnings: list[str] = []
    vulns_file = run_dir / "vulnerabilities.json"
    if vulns_file.exists():
        try:

            def reject_constant(_value: str) -> None:
                raise ValueError("invalid JSON constant")

            data = json.loads(
                vulns_file.read_text(encoding="utf-8"),
                parse_float=finite_json_float,
                parse_constant=reject_constant,
            )
            if isinstance(data, list):
                vulns = [entry for entry in data if _displayable_vulnerability(entry)]
                if len(vulns) != len(data):
                    warnings.append(
                        "Some finding records could not be displayed; the original artifacts are unchanged."
                    )
            else:
                warnings.append("The finding artifact has an unsupported format; results are unknown.")
        except (OSError, UnicodeError, ValueError, RecursionError):
            warnings.append("The finding artifact could not be read; results are unknown.")

    internal: list[dict[str, Any]] = []
    internal_dir = run_dir / "internal_findings"
    if internal_dir.is_dir():
        for path in sorted(internal_dir.glob("*.md")):
            internal.append(_parse_internal_md(path))

    result: dict[str, Any] = {"vulnerabilities": vulns, "internal": internal}
    if warnings:
        result["read_warnings"] = warnings
    return result


def _parse_internal_md(path: Path) -> dict[str, Any]:
    finding: dict[str, Any] = {"id": path.stem, "source_file": path.name}
    try:
        head = path.read_text(encoding="utf-8", errors="replace").splitlines()[:15]
    except OSError:
        return finding
    for line in head:
        if line.startswith("# ") and "title" not in finding:
            finding["title"] = line[2:].strip()
        elif line.startswith("**Type:**"):
            finding["finding_type"] = line.removeprefix("**Type:**").strip()
        elif line.startswith("**Severity:**"):
            finding["severity"] = line.removeprefix("**Severity:**").strip().upper()
        elif line.startswith("**Host:**"):
            finding["host"] = line.removeprefix("**Host:**").strip()
    return finding
