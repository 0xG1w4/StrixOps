"""Deliver operator instructions to their explicit agent in its existing session.

Empty legacy targets mean root. Explicit targets never fall back to another
agent. Queue admission and actual delivery are separate: terminal/unknown agents
produce a persisted failed record instead of a misleading permanent queue item.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from strixops.platform import hint_store as store

logger = logging.getLogger("strixops.hints")
POLL_INTERVAL_SECONDS = 2.0


class OperatorHintsPoller:
    def __init__(self, hints_dir: str | Path, coordinator: Any) -> None:
        self._dir = Path(hints_dir) if hints_dir else None
        self._coordinator = coordinator
        self._processed: set[str] = set()
        self._pending_updates: dict[str, tuple[str, dict[str, Any]]] = {}
        self._inflight: str | None = None
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Storage/model-delivery failures never abort the scan."""
        if self._dir is None:
            return
        try:
            while not self._stop.is_set():
                try:
                    await self._poll_once()
                except Exception:  # noqa: BLE001 — the scan must survive a damaged inbox
                    logger.warning("operator instruction inbox could not be processed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)
        finally:
            self._close()

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def _entries(self) -> list[tuple[str, dict[str, Any]]]:
        if self._dir is None:
            return []
        with store.inbox_directory(self._dir) as directory:
            if directory is not None and store.is_closed(directory):
                return []
            return store.read_entries(directory)

    def _flush_updates(self) -> None:
        if self._dir is None or not self._pending_updates:
            return
        try:
            with store.inbox_directory(self._dir, locked=True) as directory:
                if directory is None:
                    return
                for filename, (message_id, update) in list(self._pending_updates.items()):
                    try:
                        existing = store.read_hint(directory, filename)
                        existing_id = (
                            str(existing.get("message_id") or Path(filename).stem) if existing else ""
                        )
                        if existing is None or existing_id != message_id:
                            self._pending_updates.pop(filename, None)
                            continue
                        if str(existing.get("status") or "queued") not in store.FINAL_HINT_STATUSES:
                            store.write_hint(directory, filename, {**existing, **update})
                        self._pending_updates.pop(filename, None)
                    except (store.HintError, OSError):
                        logger.warning("operator instruction status could not be saved; retrying")
        except store.HintError:
            logger.warning("operator instruction status storage is unavailable")

    async def _poll_once(self) -> None:
        self._flush_updates()
        if self._stop.is_set():
            return
        entries = sorted(self._entries(), key=lambda item: (str(item[1].get("created_at") or ""), item[0]))
        for filename, hint in entries:
            if self._stop.is_set():
                break
            message_id = str(hint.get("message_id") or Path(filename).stem)
            already_final = str(hint.get("status") or "queued") in store.FINAL_HINT_STATUSES
            if message_id in self._processed or already_final:
                continue
            self._processed.add(message_id)
            self._inflight = filename
            try:
                try:
                    target, agent_name = await self._deliver(hint)
                    update = {
                        "status": "delivered", "agent_id": target, "agent_name": agent_name,
                        "delivered_at": store.now(),
                    }
                except store.HintError as exc:
                    update = {"status": "failed", "failure_code": exc.code, "failed_at": store.now()}
                except Exception:  # noqa: BLE001 — no provider exception or private path in the ledger
                    update = {"status": "failed", "failure_code": "delivery_failed", "failed_at": store.now()}
                self._pending_updates[filename] = (message_id, update)
            finally:
                self._inflight = None
                self._flush_updates()
                if self._stop.is_set():
                    self._close()

    async def _deliver(self, hint: dict[str, Any]) -> tuple[str, str]:
        if self._stop.is_set():
            raise store.HintError("run_not_active")
        payload = store.canonical_payload(
            str(hint.get("message") or ""), str(hint.get("agent_id") or ""),
            str(hint.get("phase") or "1"),
        )
        target = payload["agent_id"]
        root = self._coordinator.entry_of("root")
        if root is not None and root.get("status") not in store.ACTIVE_AGENT_STATUSES:
            raise store.HintError("run_not_active")
        entry = self._coordinator.entry_of(target)
        if entry is None:
            raise store.HintError("unknown_agent")
        if entry.get("status") not in store.ACTIVE_AGENT_STATUSES:
            raise store.HintError("agent_not_active")
        token = str(hint.get("hint_token") or "").strip()
        framed = (
            f"[Operator hint | token={token} | phase={payload['phase']}]\n{payload['message']}"
            if token else payload["message"]
        )
        if not await self._coordinator.deliver_hint(
            target, framed, admission_check=lambda: not self._stop.is_set()
        ):
            # The target may have completed between the checks and locked send.
            raise store.HintError("run_not_active" if self._stop.is_set() else "agent_not_active")
        return target, str(entry.get("name") or target)[:200]

    def _close(self) -> None:
        """Close admission and persist undelivered items, including shutdown races."""
        if self._dir is None:
            return
        self._flush_updates()
        try:
            with store.inbox_directory(self._dir, locked=True) as directory:
                if directory is None:
                    return
                store.close_inbox(directory)
                for filename, hint in store.read_entries(directory):
                    if filename in self._pending_updates or filename == self._inflight:
                        continue  # Never relabel an accepted message after a failed status write.
                    if str(hint.get("status") or "queued") not in store.FINAL_HINT_STATUSES:
                        store.write_hint(directory, filename, {
                            **hint, "status": "failed", "failure_code": "run_not_active",
                            "failed_at": store.now(),
                        })
        except (store.HintError, OSError):
            logger.warning("operator instruction inbox finalization could not be saved")
