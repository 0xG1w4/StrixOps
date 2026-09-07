"""The ``events.jsonl`` stream — the platform's state-machine lifeline.

Envelope (authoritative schema: the platform's unified-event contract, as
consumed by ``apps/api/services/{supervisor,strix_data,task_agents}.py``)::

    {"timestamp": "<ISO-8601 UTC>", "event_type": str,
     "actor": {"agent_id": str, "agent_name": str}, "payload": {...}}

Event vocabulary and trigger rules:

===========================  ==============================================  ========
event_type                   payload                                         rule
===========================  ==============================================  ========
``run.configured``           ``{"scan_config": {...}}``                      once at startup
``agent.created``            ``{task, parent_id, status: "running", skills}``  on register
``agent.status.updated``     ``{status, parent_id}``                         **transitions only**
``chat.message``             ``{content}``                                   SDK message output
``tool.execution.started``   ``{args}``                                      tool call begins
``tool.execution.updated``   ``{result}``                                    tool result
``vulnerability.found``      ``{finding, report_id}``                        new finding
``finding.internal_created`` ``{finding}``                                   internal finding
``run.completed``            ``{duration_seconds, vulnerability_count}``     **exactly once**
===========================  ==============================================  ========

Platform consumers pair ``agent.created`` (always ``status: "running"``) with
``agent.status.updated`` fired only on transitions — replicate that pairing
exactly. The first line of this file (any event) flips the task from
``preparing`` to ``running``; a ``run.completed`` line promotes it to
``reporting``.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class EventWriter:
    """Append-only, lock-guarded writer for ``<run_dir>/events.jsonl``.

    Safe to call from asyncio tasks and reader threads alike; a single
    ``threading.Lock`` serializes appends so lines never interleave.
    Emission never raises — losing one event line must not kill a scan.
    """

    def __init__(self, run_dir: Path) -> None:
        self._path = Path(run_dir) / "events.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def emit(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        agent_id: str = "",
        agent_name: str = "",
        timestamp: str | None = None,
    ) -> None:
        event = {
            "timestamp": timestamp or utc_now_iso(),
            "event_type": event_type,
            "actor": {"agent_id": agent_id, "agent_name": agent_name},
            "payload": payload,
        }
        line = json.dumps(event, ensure_ascii=False, default=str)
        try:
            with self._lock, self._path.open("a", encoding="utf-8") as fh:
                fh.write(f"{line}\n")
        except OSError:
            pass

    # -- agent lifecycle ---------------------------------------------------

    def agent_created(
        self,
        *,
        agent_id: str,
        agent_name: str,
        parent_id: str | None,
        task: str | None,
        skills: list[str] | None = None,
    ) -> None:
        self.emit(
            event_type="agent.created",
            payload={
                "task": task or "",
                "parent_id": parent_id,
                "status": "running",
                "skills": list(skills or []),
            },
            agent_id=agent_id,
            agent_name=agent_name,
        )

    def agent_status_changed(
        self,
        *,
        agent_id: str,
        agent_name: str,
        parent_id: str | None,
        status: str,
    ) -> None:
        self.emit(
            event_type="agent.status.updated",
            payload={"status": status, "parent_id": parent_id},
            agent_id=agent_id,
            agent_name=agent_name,
        )

    def run_configured(self, scan_config: dict[str, Any]) -> None:
        self.emit(event_type="run.configured", payload={"scan_config": scan_config})

    # -- findings ----------------------------------------------------------

    def vulnerability_found(
        self, *, finding: dict[str, Any], report_id: str, agent_id: str, agent_name: str
    ) -> None:
        self.emit(
            event_type="vulnerability.found",
            payload={"finding": finding, "report_id": report_id},
            agent_id=agent_id,
            agent_name=agent_name,
        )

    def internal_finding_created(self, *, finding: dict[str, Any], agent_id: str, agent_name: str) -> None:
        self.emit(
            event_type="finding.internal_created",
            payload={"finding": finding},
            agent_id=agent_id,
            agent_name=agent_name,
        )

    # -- SDK stream mapping --------------------------------------------------

    def sdk_event(self, agent_id: str, agent_name: str, event: Any) -> None:
        """Map an openai-agents SDK ``RunItemStreamEvent`` to platform events.

        Only ``run_item_stream_event`` items are considered; the three item
        types below map to chat/tool events. Tolerant of dict- and
        attr-shaped items (mirrors the platform's own parser).
        """
        if _field(event, "type") != "run_item_stream_event":
            return
        item = _field(event, "item", None)
        if item is None:
            return
        item_type = _field(item, "type")

        if item_type == "message_output_item":
            content = _message_text(item)
            if content and content.strip():
                self.emit(
                    event_type="chat.message",
                    payload={"content": content},
                    agent_id=agent_id,
                    agent_name=agent_name,
                )
            return

        if item_type == "tool_call_item":
            raw = _field(item, "raw_item", None)
            self.emit(
                event_type="tool.execution.started",
                payload={"args": _tool_arguments(raw)},
                agent_id=agent_id,
                agent_name=agent_name,
            )
            return

        if item_type == "tool_call_output_item":
            output = _field(item, "output", None)
            self.emit(
                event_type="tool.execution.updated",
                payload={"result": _parse_json_value(output)},
                agent_id=agent_id,
                agent_name=agent_name,
            )


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _message_text(item: Any) -> str:
    raw = _field(item, "raw_item", None)
    content = _field(raw, "content", [])
    parts: list[str] = []
    items = content if isinstance(content, list) else [content]
    for part in items:
        if isinstance(part, str):
            parts.append(part)
            continue
        text = _field(part, "text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _json_object(value: Any) -> dict[str, Any]:
    parsed = _parse_json_value(value)
    return parsed if isinstance(parsed, dict) else {}


def _tool_arguments(raw: Any) -> dict[str, Any]:
    args = _json_object(_field(raw, "arguments"))
    # The platform classifies proxy events by argument keys, not tool names.
    # Preserve that contract when the model omits these optional parameters.
    # The other proxy tools already require request_id or entry_id.
    name = _field(raw, "name")
    if name == "list_requests":
        return {"httpql_filter": None, **args}
    if name in ("list_sitemap", "scope_rules"):
        return {"scope_id": None, **args}
    return args


def _parse_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
