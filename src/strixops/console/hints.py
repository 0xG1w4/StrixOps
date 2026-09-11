"""Console admission and idempotent creation of per-task operator instructions."""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from strixops.platform import hint_store as store


def read_agents(run_dir: Path, open_file: Callable[[Path, str], int]) -> dict[str, Any]:
    try:
        with os.fdopen(open_file(run_dir, ".state/agents.json"), "rb") as handle:
            if os.fstat(handle.fileno()).st_nlink != 1:
                raise store.HintError("storage_unavailable")
            raw = handle.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise store.HintError("storage_unavailable")
        agents = json.loads(raw)
        if not isinstance(agents, dict):
            raise ValueError
        return agents
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise store.HintError("storage_unavailable") from None


def send_result(hint: dict[str, Any]) -> dict[str, Any]:
    projected = store.project_hint(hint)
    keys = ("message_id", "hint_token", "agent_id", "status", "failure_code", "failure_reason")
    return {"ok": True, **{key: projected[key] for key in keys if key in projected}}


def enqueue(
    run_dir: Path,
    *,
    message: str,
    agent_id: str,
    phase: str,
    client_request_id: str | None,
    admission: Callable[[str], str],
) -> dict[str, Any]:
    """A retry returns its prior result even if the task has since completed."""
    payload = store.canonical_payload(message, agent_id, phase)
    request_id = store.canonical_request_id(client_request_id)
    filename = f"request_{request_id}.json" if request_id else f"hint_{uuid.uuid4().hex}.json"
    with store.inbox_directory(run_dir / "operator_hints", create=True, locked=True) as directory:
        assert directory is not None
        if request_id:
            existing = store.read_hint(directory, filename)
            if existing is not None:
                existing_payload = {key: existing.get(key) for key in payload}
                if existing.get("client_request_id") != request_id or existing_payload != payload:
                    raise store.HintError("idempotency_conflict")
                return send_result(existing)
        if store.is_closed(directory):
            raise store.HintError("run_not_active")
        agent_name = admission(payload["agent_id"])
        entries = store.read_entries(directory)
        if len(entries) >= store.MAX_HINTS:
            raise store.HintError("inbox_limit")
        hint = {
            **payload, "message_id": uuid.uuid4().hex[:12], "agent_name": agent_name,
            "created_at": store.now(), "status": "queued", "hint_token": secrets.token_hex(4),
            "task_id": "", "target": "",
        }
        if request_id:
            hint["client_request_id"] = request_id
        total = sum(len(json.dumps(item, ensure_ascii=False).encode("utf-8")) for _, item in entries)
        if total + len(json.dumps(hint, ensure_ascii=False).encode("utf-8")) > store.MAX_INBOX_BYTES:
            raise store.HintError("inbox_limit")
        store.write_hint(directory, filename, hint)
        return send_result(hint)
