"""Operator-hints inbox poller — the read side the platform never had.

The platform writes hint JSON files into ``STRIX_OPERATOR_HINTS_DIR``
(``apps/api/services/operator_hints.py``: atomic ``.tmp``→rename, payload
``{message_id, task_id, phase, target, agent_id, agent_name, message,
created_at, status, hint_token}``). No strix version ever read them — this
is StrixOps' implementation:

* poll every 2 s; skip ``*.tmp`` and already-processed ``message_id``s
* route by ``agent_id`` when it names a registered agent, else to root
* deliver through the coordinator with force-interrupt so a mid-scan agent
  sees the hint immediately (non-interactive agents otherwise drain
  mailboxes only at ``wait_for_agents``)
* flip the hint file's ``status`` to ``delivered`` after a successful
  delivery, so the console can observe queued → delivered → acked
* frame the content with the hint token; the system prompt instructs the
  agent to echo the token in its next message, which the platform's
  token-echo detection consumes to mark the hint delivered/acked
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("strixops.hints")

POLL_INTERVAL_SECONDS = 2.0


class OperatorHintsPoller:
    def __init__(self, hints_dir: str | Path, coordinator: Any) -> None:
        self._dir = Path(hints_dir) if hints_dir else None
        self._coordinator = coordinator
        self._processed: set[str] = set()
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Poll until stopped. Delivery failures are logged, never fatal."""
        if self._dir is None:
            return
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception as exc:  # noqa: BLE001 — poller must survive anything
                logger.warning("hints poll failed: %s", exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    async def _poll_once(self) -> None:
        if not self._dir.is_dir():
            return
        candidates: list[tuple[str, Path]] = []
        for path in sorted(self._dir.glob("*.json")):
            if path.name.endswith(".tmp"):
                continue
            hint = self._read_hint(path)
            if hint is None:
                continue
            message_id = str(hint.get("message_id") or path.stem)
            if message_id in self._processed:
                continue
            self._processed.add(message_id)
            created = str(hint.get("created_at") or "")
            candidates.append((created, path, hint))  # type: ignore[misc]

        for _, path, hint in sorted(candidates, key=lambda item: item[0]):
            if await self._deliver(hint):
                self._mark_delivered(path, hint)

    def _read_hint(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    async def _deliver(self, hint: dict[str, Any]) -> bool:
        """Deliver one hint; True when the target agent (or root) accepted it."""
        message = str(hint.get("message") or "").strip()
        if not message:
            return False
        token = str(hint.get("hint_token") or "").strip()
        phase = str(hint.get("phase") or "").strip()
        agent_id = str(hint.get("agent_id") or "").strip()

        framed = f"[Operator hint | token={token} | phase={phase}]\n{message}" if token else message

        target = agent_id if agent_id and self._coordinator.entry_of(agent_id) else "root"
        delivered = await self._coordinator.deliver_hint(target, framed)
        if not delivered and target != "root":
            # Unknown target — fall back to root rather than dropping the hint.
            target = "root"
            delivered = await self._coordinator.deliver_hint(target, framed)
        if delivered:
            logger.info(
                "delivered operator hint %s to %s (token=%s)",
                hint.get("message_id"),
                target,
                token,
            )
            return True
        logger.warning("operator hint %s could not be delivered", hint.get("message_id"))
        return False

    def _mark_delivered(self, path: Path, hint: dict[str, Any]) -> None:
        """Flip the hint file's status to ``delivered`` (atomic rewrite, best-effort).

        The console's GET /api/runs/{name}/hints reads this field; the queued →
        delivered transition is what makes delivery observable. Token-echo
        detection upgrades it further to ``acked`` on the console side.
        """
        try:
            updated = dict(hint, status="delivered")
            tmp = path.with_name(f"{path.name}.tmp")
            tmp.write_text(json.dumps(updated, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.debug("could not mark hint %s delivered", hint.get("message_id"))
