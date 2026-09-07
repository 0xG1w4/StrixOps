"""Frozen copies of the platform's read-side oracles.

These are VERBATIM copies of:

* ``apps/api/services/run_resolver.py`` — ``candidate_run_prefixes``,
  ``run_name_matches``, ``_slugify_for_strix_dev``
* ``apps/api/services/strix_data.py`` — ``_infer_unified_tool_context``

They are the platform's actual matching/classification logic. StrixOps
output must satisfy them. When the platform changes these functions, re-freeze
deliberately — a silent drift here is a silent integration break there.
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------
# run_resolver.py (verbatim)
# --------------------------------------------------------------------------

_RUN_SUFFIX_RE = re.compile(r"^.+_[0-9a-fA-F]{4}$")


def candidate_run_prefixes(*values: str | None) -> list[str]:
    seen: set[str] = set()
    prefixes: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        variants = {
            text,
            text.replace("_", "-"),
            text.replace("_", "."),
        }
        if "://" not in text:
            variants.add(f"https://{text.replace('_', '.')}")
            variants.add(f"http://{text.replace('_', '.')}")
        for variant in variants:
            for prefix in (variant.replace("_", "-"), _slugify_for_strix_dev(variant)):
                prefix = prefix.strip("-_")
                if prefix and prefix not in seen:
                    seen.add(prefix)
                    prefixes.append(prefix)
    return prefixes


def run_name_matches(name: str, *values: str | None) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    for prefix in candidate_run_prefixes(*values):  # noqa: SIM110 — verbatim platform copy
        if text == prefix or text.startswith(f"{prefix}_"):
            return True
    return False


def _slugify_for_strix_dev(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug[:32]


def looks_like_run_dir(name: str) -> bool:
    return bool(_RUN_SUFFIX_RE.match(name))


# --------------------------------------------------------------------------
# strix_data.py:_infer_unified_tool_context (verbatim)
# --------------------------------------------------------------------------


def infer_unified_tool_context(args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {}

    if any(key in args for key in ("command", "cmd", "chars", "session_id")):
        return {
            "kind": "shell",
            "command": str(args.get("command") or args.get("cmd") or args.get("chars") or "").strip(),
        }

    if any(key in args for key in ("todos", "todo_ids", "updates")) or (
        any(key in args for key in ("status", "priority"))
        and set(args.keys()).issubset({"status", "priority"})
    ):
        todo_action = ""
        if "todos" in args:
            todo_action = "created"
        elif "updates" in args:
            todo_action = "updated"
        elif "todo_ids" in args:
            todo_action = "marked"
        elif "status" in args or "priority" in args:
            todo_action = "listed"
        return {"kind": "todo", "todo_action": todo_action}

    if all(
        key in args for key in ("executive_summary", "methodology", "technical_analysis", "recommendations")
    ):
        return {"kind": "finish"}

    if "title" in args and any(
        key in args for key in ("description", "impact", "technical_analysis", "endpoint", "target")
    ):
        return {"kind": "vulnerability_report", "title": str(args.get("title") or "").strip()}

    if "result_summary" in args:
        return {"kind": "agent_finish", "result_summary": str(args.get("result_summary") or "").strip()}

    if any(
        key in args
        for key in (
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
        )
    ):
        return {"kind": "proxy"}

    return {}
