"""One locked scheduler; callbacks reuse the application's existing launcher."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from dataclasses import dataclass

from .processes import process_alive
from .store import TERMINAL, QueueError, QueueStore


@dataclass(frozen=True)
class LaunchReceipt:
    pid: int
    run_name: str
    run_dir: str
    start_identity: str = ""


@dataclass(frozen=True)
class RunObservation:
    alive: bool | None
    status: str = ""
    cleanup_complete: bool = False
    report_ready: bool = False
    error_code: str = ""


class BatchScheduler:
    def __init__(
        self,
        store: QueueStore,
        *,
        launch: Callable[[dict], LaunchReceipt],
        inspect: Callable[[dict], RunObservation],
        cancel: Callable[[dict], None],
        validate_launch: Callable[[dict], None],
        owner: str = "default",
    ) -> None:
        self.store, self.launch, self.inspect = store, launch, inspect
        self.cancel, self.validate_launch = cancel, validate_launch
        self.owner = owner

    def tick(self) -> None:
        path = self.store.path.with_suffix(self.store.path.suffix + ".scheduler.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            self.store.recover_orphaned_leases()
            for item in self.store.pending_items(owner=self.owner):
                self._reconcile(item)
            # Bounded by host capacity, even if callbacks immediately exit.
            for _ in range(16):
                item = self.store.claim_next(owner=self.owner)
                if item is None:
                    break
                try:
                    self.validate_launch(item)
                except Exception as exc:
                    code = exc.code if isinstance(exc, QueueError) else "queue_snapshot_invalid"
                    self.store.update_item(item["id"], status="failed", error_code=code)
                    continue
                fresh = self.store.internal_item(item["id"])
                if fresh["cancel_requested"]:
                    self.store.update_item(item["id"], status="cancelled")
                    continue
                try:
                    receipt = self.launch(fresh)
                    self.store.bind_launch(item["id"], receipt)
                except Exception as exc:
                    # Callback may have spawned before an I/O error. Retain the
                    # reservation; a child registering its token can be recovered.
                    no_spawn = isinstance(exc, QueueError) and exc.code == "queue_launch_failed"
                    self.store.update_item(
                        item["id"],
                        status="failed" if no_spawn else "blocked",
                        error_code="queue_launch_failed" if no_spawn else "queue_launch_uncertain",
                    )
        finally:
            os.close(fd)

    def _reconcile(self, item: dict) -> None:
        if self.store.fail_unstarted_orphan(item["id"]):
            return
        if not item.get("pid"):
            lease = self.store.lease_state(item["id"])
            if lease is not None:
                item = self.store.internal_item(item["id"])
            if not item.get("pid"):
                self.store.update_item(item["id"], status="blocked", error_code="queue_launch_uncertain")
                return
        actual = process_alive(item["pid"], item.get("start_identity") or "")
        try:
            observed = self.inspect(item)
        except Exception:
            observed = RunObservation(alive=actual)
        # Inspect must verify the same PID identity; never downgrade uncertainty
        # merely because a callback saw a PID belonging to a different process.
        alive = actual if actual is not None else observed.alive
        lease = self.store.lease_state(item["id"])
        cleanup = bool(observed.cleanup_complete or (lease and lease["cleanup_complete"]))
        if alive is False:
            if cleanup:
                status = (
                    "cancelled"
                    if item["cancel_requested"]
                    else (observed.status if observed.status in TERMINAL else "failed")
                )
                self.store.update_item(
                    item["id"],
                    status=status,
                    report_ready=observed.report_ready,
                    error_code=(observed.error_code or "queue_scan_failed") if status == "failed" else "",
                )
            else:
                self.store.update_item(
                    item["id"],
                    status="blocked",
                    error_code="queue_cleanup_unconfirmed",
                    report_ready=observed.report_ready,
                )
            return
        if alive is None:
            self.store.update_item(item["id"], status="blocked", error_code="queue_process_unconfirmed")
            return
        if item["cancel_requested"]:
            self.store.update_item(item["id"], status="cancelling", report_ready=observed.report_ready)
            if not item["stop_sent"]:
                try:
                    self.cancel(item)
                    self.store.mark_stop_sent(item["id"])
                except Exception:
                    self.store.update_item(item["id"], status="cancelling", error_code="queue_stop_failed")
        elif lease and lease["state"] in {"active", "draining"}:
            self.store.update_item(item["id"], status="running", report_ready=observed.report_ready)
