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
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strixops.engine.stream_cleanup import cancel_stream, join_task, settle_stream, stream_tasks
from strixops.platform.events import EventWriter

STATUS_RUNNING = "running"
STATUS_WAITING = "waiting"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CRASHED = "crashed"
STATUS_STOPPED = "stopped"

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CRASHED, STATUS_STOPPED}


@dataclass(frozen=True)
class CoordinationLimits:
    """Root is depth zero and counts toward the active and lifetime limits."""

    max_depth: int = 2
    max_active: int = 4
    max_total: int = 12

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 1
            for value in (
                self.max_depth,
                self.max_active,
                self.max_total,
            )
        ):
            raise ValueError("Agent limits must be positive integers")
        if self.max_active > self.max_total:
            raise ValueError("Agent active limit cannot exceed the lifetime limit")

    @classmethod
    def from_env(cls) -> CoordinationLimits:
        return cls(
            **{
                name: int(os.environ.get(f"STRIXOPS_AGENT_{name.upper()}", default))
                for name, default in (("max_depth", 2), ("max_active", 4), ("max_total", 12))
            }
        )


class AdmissionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AgentCoordinator:
    def __init__(
        self,
        run_dir: Path,
        events: EventWriter,
        *,
        limits: CoordinationLimits | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.events = events
        self._lock = asyncio.Lock()
        self._agents: dict[str, dict[str, Any]] = {}
        self._mailboxes: dict[str, list[dict[str, Any]]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._streams: dict[str, Any] = {}
        self.limits = limits or CoordinationLimits.from_env()
        self._finishing: set[str] = set()
        self._closing = False
        self._quiesce_task: asyncio.Task | None = None
        self._cancellations: dict[str, asyncio.Task] = {}

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
        self.check_admission(parent_id)
        self._agents[agent_id] = {
            "name": name,
            "task": task or "",
            "status": STATUS_RUNNING,
            "parent_id": parent_id,
            "skills": list(skills or []),
            "depth": self.depth_of(parent_id) + 1 if parent_id is not None else 0,
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

    def check_admission(self, parent_id: str | None) -> None:
        if self._closing:
            raise AdmissionError("CLOSING", "The scan is finishing; no new agents may start.")
        if parent_id is not None:
            current: str | None = parent_id
            while current is not None:
                entry = self._agents.get(current)
                if entry is None or entry["status"] in TERMINAL_STATUSES or current in self._finishing:
                    raise AdmissionError("INACTIVE_PARENT", "The parent or an ancestor is no longer active.")
                current = entry["parent_id"]
            if self.depth_of(parent_id) + 1 > self.limits.max_depth:
                raise AdmissionError(
                    "MAX_DEPTH", f"Maximum agent depth is {self.limits.max_depth} (root is 0)."
                )
        elif self._agents:
            raise AdmissionError("ROOT_EXISTS", "The run already has a root agent.")
        if len(self._agents) >= self.limits.max_total:
            raise AdmissionError(
                "MAX_TOTAL", f"Lifetime agent limit is {self.limits.max_total}, including root."
            )
        if len(self.active_ids()) >= self.limits.max_active:
            raise AdmissionError(
                "MAX_ACTIVE", f"Active agent limit is {self.limits.max_active}, including root; wait first."
            )

    def depth_of(self, agent_id: str | None) -> int:
        return int((self._agents.get(agent_id or "") or {}).get("depth", 0))

    def descendants_of(self, agent_id: str) -> list[str]:
        descendants: list[str] = []
        frontier = [agent_id]
        while frontier:
            children = self.children_of(frontier.pop())
            descendants.extend(children)
            frontier.extend(children)
        return descendants

    def is_active(self, agent_id: str) -> bool:
        entry = self._agents.get(agent_id)
        if entry is None:
            return False
        task = self._tasks.get(agent_id)
        stream = self._streams.get(agent_id)
        return (
            entry["status"] not in TERMINAL_STATUSES
            or (task is not None and not task.done())
            or (stream is not None and any(not t.done() for t in stream_tasks(stream)))
        )

    def active_descendants(self, agent_id: str) -> list[str]:
        return [child for child in self.descendants_of(agent_id) if self.is_active(child)]

    def begin_finish(self, agent_id: str) -> bool:
        """Synchronous finish reservation excludes concurrent child admission."""
        entry = self._agents.get(agent_id)
        if (
            self._closing
            or entry is None
            or entry["status"] in TERMINAL_STATUSES
            or agent_id in self._finishing
            or self.active_descendants(agent_id)
        ):
            return False
        self._finishing.add(agent_id)
        return True

    def abort_finish(self, agent_id: str) -> None:
        self._finishing.discard(agent_id)

    async def cancel_agent(self, agent_id: str) -> bool:
        task = self._tasks.get(agent_id)
        if task is None or task.done():
            return False
        self._finishing.add(agent_id)
        if agent_id not in self._cancellations:
            if not task.cancelling():
                task.cancel()

            async def stop() -> None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                await self.set_status(agent_id, STATUS_STOPPED)

            self._cancellations[agent_id] = asyncio.create_task(stop(), name=f"stop-agent-{agent_id}")
        interrupted = await join_task(self._cancellations[agent_id])
        if interrupted:
            raise asyncio.CancelledError
        return True

    def close_admission(self) -> None:
        self._closing = True

    async def quiesce(self) -> None:
        """Close admission and join all child writers before shared teardown."""
        self.close_admission()
        if self._quiesce_task is None:
            # Request cancellation before scheduling the cleanup task, so a
            # just-reserved child cannot start while shutdown is being queued.
            for agent_id, task in self._tasks.items():
                self._finishing.add(agent_id)
                if not task.done() and not task.cancelling():
                    task.cancel()

            async def stop_all() -> None:
                # Parent cancellation prevents new work; admission is already
                # closed before yielding. Child loops join their own streams.
                for agent_id in list(self._tasks):
                    await self.cancel_agent(agent_id)
                for agent_id, stream in list(self._streams.items()):
                    await settle_stream(stream, cancel=True)
                    self.detach_stream(agent_id)
                for agent_id, entry in list(self._agents.items()):
                    if entry["parent_id"] is not None and entry["status"] not in TERMINAL_STATUSES:
                        await self.set_status(agent_id, STATUS_STOPPED)

            self._quiesce_task = asyncio.create_task(stop_all(), name="strixops-agent-quiesce")
        interrupted = await join_task(self._quiesce_task)
        if interrupted:
            raise asyncio.CancelledError

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
                cancel_stream(stream)
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
            if self.is_active(agent_id) and agent_id != exclude
        ]

    def terminal_ids(self) -> list[str]:
        return [agent_id for agent_id, entry in self._agents.items() if entry["status"] in TERMINAL_STATUSES]

    # -- mailboxes ---------------------------------------------------------

    async def send(self, to_id: str, message: dict[str, Any]) -> bool:
        async with self._lock:
            entry = self._agents.get(to_id)
            if entry is None or entry["status"] in TERMINAL_STATUSES or to_id in self._finishing:
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
                "depth": entry["depth"],
                "skills": entry["skills"],
            }
            for agent_id, entry in self._agents.items()
        }
        path = self.run_dir / ".state" / "agents.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
