"""Agent coordinator — registry, lineage, mailboxes, wake, snapshots.

Platform contract (pairing matters):

* ``register()`` emits ``agent.created`` with ``status: "running"``.
* ``set_status()`` emits ``agent.status.updated`` **only on transitions** —
  the platform's task-agents rebuild and TUI mirror assume no-repeat events.
* Snapshots land in ``.state/agents.json`` via atomic replace.

Single owner of all agent bookkeeping on the shared event loop; async updates
use one ``asyncio.Lock``. Synchronous reservations never yield. Mailboxes
are simple per-agent queues drained explicitly (by ``wait_for_agents`` or on
wake). Messages are plain dicts: ``{"from", "from_name", "type",
"priority", "content"}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from strixops.platform.events import EventWriter

STATUS_RUNNING = "running"
STATUS_WAITING = "waiting"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CRASHED = "crashed"
STATUS_STOPPED = "stopped"

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CRASHED, STATUS_STOPPED}


class AgentCoordinator:
    def __init__(self, run_dir: Path, events: EventWriter) -> None:
        self.run_dir = Path(run_dir)
        self.events = events
        self._lock = asyncio.Lock()
        self._agents: dict[str, dict[str, Any]] = {}
        self._mailboxes: dict[str, list[dict[str, Any]]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._streams: dict[str, Any] = {}

    # -- lifecycle ----------------------------------------------------------

    def reserve(
        self,
        agent_id: str,
        name: str,
        *,
        parent_id: str | None,
        task: str | None,
        skills: list[str] | None = None,
    ) -> None:
        """Publish a child before scheduling it, without yielding to its parent.

        The running entry immediately participates in finish gates, lineage,
        messaging and teardown. The child persists the snapshot when it starts;
        cancellation before startup persists its terminal status instead.
        """
        if agent_id in self._agents:
            raise ValueError(f"Agent {agent_id} is already registered")
        self._agents[agent_id] = {
            "name": name,
            "task": task or "",
            "status": STATUS_RUNNING,
            "parent_id": parent_id,
            "skills": list(skills or []),
        }
        self.events.agent_created(
            agent_id=agent_id,
            agent_name=name,
            parent_id=parent_id,
            task=task,
            skills=skills,
        )

    async def register(
        self,
        agent_id: str,
        name: str,
        *,
        parent_id: str | None,
        task: str | None,
        skills: list[str] | None = None,
    ) -> None:
        async with self._lock:
            self.reserve(agent_id, name, parent_id=parent_id, task=task, skills=skills)
        await self.snapshot()

    async def set_status(self, agent_id: str, status: str) -> None:
        async with self._lock:
            entry = self._agents.get(agent_id)
            if entry is None or entry["status"] == status:
                return
            entry["status"] = status
            name = entry["name"]
            parent_id = entry["parent_id"]
        self.events.agent_status_changed(
            agent_id=agent_id,
            agent_name=name,
            parent_id=parent_id,
            status=status,
        )
        await self.snapshot()

    # -- task bookkeeping -----------------------------------------------------

    def attach_task(self, agent_id: str, task: asyncio.Task) -> None:
        self._tasks[agent_id] = task

    async def cancel_agent(self, agent_id: str) -> bool:
        task = self._tasks.get(agent_id)
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await self.set_status(agent_id, STATUS_STOPPED)
        return True

    # -- stream interruption (operator hints) ---------------------------------

    def attach_stream(self, agent_id: str, stream: Any) -> None:
        self._streams[agent_id] = stream

    def detach_stream(self, agent_id: str) -> None:
        self._streams.pop(agent_id, None)

    async def deliver_hint(self, agent_id: str, content: str) -> bool:
        """Mail a hint to an agent and interrupt its active turn.

        Non-interactive agents drain mailboxes only at ``wait_for_agents``,
        so an operator hint must also cancel the in-flight stream
        (``immediate``); the loop then replays with the mailbox contents.
        """
        sent = await self.send(
            agent_id,
            {
                "from": "operator",
                "from_name": "operator",
                "type": "operator_hint",
                "priority": "high",
                "content": content,
            },
        )
        if not sent:
            return False
        stream = self._streams.get(agent_id)
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.cancel("immediate")
        return True

    # -- lineage ---------------------------------------------------------------

    def name_of(self, agent_id: str) -> str:
        entry = self._agents.get(agent_id)
        return entry["name"] if entry else agent_id

    def entry_of(self, agent_id: str) -> dict[str, Any] | None:
        return self._agents.get(agent_id)

    def agent_ids(self) -> list[str]:
        return list(self._agents)

    def children_of(self, agent_id: str) -> list[str]:
        return [other_id for other_id, entry in self._agents.items() if entry.get("parent_id") == agent_id]

    def active_ids(self, *, exclude: str | None = None) -> list[str]:
        return [
            agent_id
            for agent_id, entry in self._agents.items()
            if entry["status"] not in TERMINAL_STATUSES and agent_id != exclude
        ]

    def terminal_ids(self) -> list[str]:
        return [agent_id for agent_id, entry in self._agents.items() if entry["status"] in TERMINAL_STATUSES]

    # -- mailboxes ---------------------------------------------------------

    async def send(self, to_id: str, message: dict[str, Any]) -> bool:
        async with self._lock:
            if to_id not in self._agents:
                return False
            self._mailboxes.setdefault(to_id, []).append(dict(message))
        return True

    def pending_messages(self, agent_id: str) -> list[dict[str, Any]]:
        return list(self._mailboxes.get(agent_id, []))

    def drain_messages(self, agent_id: str) -> list[dict[str, Any]]:
        messages = self._mailboxes.pop(agent_id, [])
        return messages

    # -- snapshot -------------------------------------------------------------

    async def snapshot(self) -> None:
        payload = {
            agent_id: {
                "name": entry["name"],
                "task": entry["task"],
                "status": entry["status"],
                "parent_id": entry["parent_id"],
            }
            for agent_id, entry in self._agents.items()
        }
        path = self.run_dir / ".state" / "agents.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
