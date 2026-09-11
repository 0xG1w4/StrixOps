"""Cancellation-safe capacity acquisition before model/container startup."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

from .processes import process_identity
from .store import QueueStore


class RunLease:
    def __init__(self, run_name: str, run_dir: Path, *, store: QueueStore | None = None) -> None:
        self.store = store or QueueStore()
        self.token = uuid.uuid4().hex
        self.run_name, self.run_dir = run_name, str(run_dir.absolute())
        self.item_id = os.environ.get("STRIXOPS_QUEUE_ITEM_ID", "")
        self.item_token = os.environ.get("STRIXOPS_QUEUE_ITEM_TOKEN", "")
        self.pid, self.identity = os.getpid(), process_identity(os.getpid())
        self.acquired = False
        self.process_boundary = os.environ.get("STRIXOPS_QUEUE_PROCESS_BOUNDARY") == "1"
        if self.item_id:
            self.store.register_worker(
                item_id=self.item_id,
                item_token=self.item_token,
                run_name=self.run_name,
                run_dir=self.run_dir,
                pid=self.pid,
                start_identity=self.identity,
            )

    async def acquire(self) -> None:
        last_recovery = 0.0
        while not self.store.try_acquire(
            token=self.token,
            run_name=self.run_name,
            run_dir=self.run_dir,
            pid=self.pid,
            start_identity=self.identity,
            item_id=self.item_id,
            item_token=self.item_token,
        ):
            if time.monotonic() - last_recovery >= 10:
                # Recovery only operates on confirmed-dead foreign processes;
                # cancellation cannot create or acquire resources in this worker.
                await asyncio.to_thread(self.store.recover_orphaned_leases)
                last_recovery = time.monotonic()
            # Keep the write synchronous: cancellation cannot abandon a worker
            # thread which then acquires a slot after the finalizer ran.
            await asyncio.sleep(0.5)
        self.acquired = True

    def finish(self, *, cleanup_complete: bool) -> None:
        self.store.finish_lease(
            self.token, cleanup_complete=cleanup_complete, process_boundary=self.process_boundary
        )

    def register_resource(self, *, owner: str, container: str = "", daemon: str = "") -> None:
        self.store.register_resource(self.token, owner=owner, container=container, daemon=daemon)
