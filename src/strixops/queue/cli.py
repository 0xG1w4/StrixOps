"""CLI batch supervisor: every target runs in its own original scan process."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import signal
import subprocess
import sys
from pathlib import Path

from strixops.config.settings import EngineSettings
from strixops.engine.scanconfig import ScanSpec
from strixops.platform.runname import resolve_runs_root

from .processes import process_alive, process_identity
from .scheduler import BatchScheduler, LaunchReceipt, RunObservation
from .store import TERMINAL, QueueError, QueueStore

CLI_OWNER = "cli:v1"


class CliBatchController:
    def __init__(self, store: QueueStore) -> None:
        # These small helpers import no Console server and expose no credentials
        # through the run/artifact tree. Console and CLI retain separate owners.
        from strixops.console.batch_launch import LaunchSnapshots

        self.store = store
        self.owner = ""
        self.snapshots = LaunchSnapshots(store.path.parent / "scan_queue_snapshots")
        self.processes: dict[str, subprocess.Popen] = {}
        self.scheduler = BatchScheduler(
            store,
            launch=self.launch,
            inspect=self.inspect,
            cancel=self.cancel,
            validate_launch=self.validate_launch,
            owner="cli:unassigned",
        )

    def create(
        self, spec: ScanSpec, settings: EngineSettings, *, max_concurrent: int = 2, name: str = ""
    ) -> dict:
        from strixops.console.batch_launch import _capture_resources

        spec = dataclasses.replace(spec)
        spec.load_instruction()
        settings = dataclasses.replace(
            settings, strix_runs=str(resolve_runs_root(settings.strix_runs).resolve())
        )
        snapshot = self.snapshots.save(
            {
                "executor": CLI_OWNER,
                "spec": dataclasses.asdict(spec),
                "settings": dataclasses.asdict(settings),
                "resources": _capture_resources(),
                "environment": {
                    key: value
                    for key, value in os.environ.items()
                    if key.startswith(("STRIX", "LLM_", "PERPLEXITY", "OPENAI_", "LITELLM_"))
                    and not key.startswith("STRIXOPS_QUEUE_")
                },
            }
        )
        self.owner = f"{CLI_OWNER}:{snapshot}"
        self.scheduler.owner = self.owner
        try:
            return self.store.create_batch(
                targets=spec.all_targets(),
                scan_type=spec.scan_type,
                snapshot_ref=snapshot,
                name=name,
                max_concurrent=max_concurrent,
                owner=self.owner,
            )
        except BaseException:
            self.snapshots.delete(snapshot)
            raise

    def _snapshot(self, item: dict) -> dict:
        snapshot = self.snapshots.read(item["snapshot_ref"])
        if snapshot.get("executor") != CLI_OWNER:
            raise QueueError("queue_snapshot_invalid")
        return snapshot

    def bind_existing_batch(self, batch_id: str) -> None:
        batch = self.store.get_batch(batch_id)
        item = self.store.internal_item(batch["items"][0]["id"])
        expected = f"{CLI_OWNER}:{item['snapshot_ref']}"
        if item["owner"] != expected:
            raise QueueError("queue_not_found")
        self._snapshot(item)
        self.owner = expected
        self.scheduler.owner = expected

    def validate_launch(self, item: dict) -> None:
        snapshot = self._snapshot(item)
        spec = ScanSpec(**snapshot["spec"])
        spec = dataclasses.replace(spec, target=item["target"], targets=[])
        if spec.validate() or EngineSettings(**snapshot["settings"]).validate():
            raise QueueError("queue_snapshot_invalid")

    def launch(self, item: dict) -> LaunchReceipt:
        try:
            return self._launch(item)
        except Exception as exc:
            if item["id"] not in self.processes:
                raise QueueError("queue_launch_failed") from exc
            raise

    def _launch(self, item: dict) -> LaunchReceipt:
        from strixops.console.batch_launch import _publish_resources

        snapshot = self._snapshot(item)
        run_dir = Path(snapshot["settings"]["strix_runs"]) / item["run_name"]
        run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        (run_dir / ".state").mkdir(mode=0o700)
        (run_dir / "operator_hints").mkdir(mode=0o700)
        spec = dataclasses.replace(
            ScanSpec(**snapshot["spec"]), target=item["target"], targets=[], instruction_file=""
        )
        _publish_resources(run_dir, snapshot["resources"], spec)
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("STRIX", "LLM_", "PERPLEXITY", "OPENAI_", "LITELLM_")):
                env.pop(key)
        env.update(snapshot["environment"])
        env.update(
            {
                "STRIXOPS_QUEUE_DB": str(self.store.path),
                "STRIXOPS_QUEUE_ITEM_ID": item["id"],
                "STRIXOPS_QUEUE_ITEM_TOKEN": item["launch_token"],
                "STRIXOPS_RUN_NAME": item["run_name"],
                "STRIXOPS_QUEUE_PROCESS_BOUNDARY": "1",
                "STRIX_RUNS": str(run_dir.parent),
                "STRIX_OPERATOR_HINTS_DIR": str(run_dir / "operator_hints"),
                "STRIX_HOST_WORKSPACE_DIR": str(run_dir / "workspace"),
            }
        )
        with (run_dir / "engine.log").open("ab") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "strixops.queue.worker", "--item", item["id"]],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.processes[item["id"]] = process
        with contextlib.suppress(OSError):
            (run_dir / ".console.pid").write_text(str(process.pid), encoding="utf-8")
        return LaunchReceipt(process.pid, item["run_name"], str(run_dir), process_identity(process.pid))

    def inspect(self, item: dict) -> RunObservation:
        import json

        process = self.processes.get(item["id"])
        if process is not None:
            process.poll()  # Reap owned children without confusing zombies for living scans.
        alive = process_alive(item.get("pid"), item.get("start_identity") or "")
        snapshot = self._snapshot(item)
        run_dir = Path(snapshot["settings"]["strix_runs"]) / item["run_name"]
        try:
            record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        return RunObservation(
            alive=alive,
            status=record.get("status", ""),
            cleanup_complete=(record.get("queue") or {}).get("cleanup_complete") is True,
            report_ready=(run_dir / "penetration_test_report.md").is_file(),
        )

    def cancel(self, item: dict) -> None:
        pid, identity = item.get("pid"), item.get("start_identity") or ""
        if process_alive(pid, identity) is not True:
            return
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


async def supervise_batch(controller: CliBatchController, batch_id: str) -> int:
    cancelled = False
    previous = None
    while True:
        try:
            controller.scheduler.tick()
            batch = controller.store.get_batch(batch_id, owner=controller.owner)
            counts = batch["counts"]
            if counts != previous:
                print(
                    f"Batch {batch_id}: " + ", ".join(f"{key}={value}" for key, value in counts.items()),
                    flush=True,
                )
                previous = counts
            if all(item["status"] in TERMINAL | {"blocked"} for item in batch["items"]):
                if cancelled:
                    raise asyncio.CancelledError
                return 0 if counts["completed"] == batch["target_count"] else 1
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            if cancelled and all(item["status"] in TERMINAL | {"blocked"} for item in batch["items"]):
                raise
            cancelled = True
            controller.store.cancel_batch(batch_id, owner=controller.owner)
            # Subsequent SIGTERM cancels only this waiter. Continue supervising
            # the original child finalizers until resource cleanup settles.


async def run_batch(
    spec: ScanSpec, settings: EngineSettings, *, max_concurrent: int = 2, name: str = ""
) -> int:
    controller = CliBatchController(QueueStore())
    batch = controller.create(spec, settings, max_concurrent=max_concurrent, name=name)
    print(f"Created batch {batch['id']} with {batch['target_count']} independent target runs.", flush=True)
    return await supervise_batch(controller, batch["id"])


async def resume_batch(batch_id: str) -> int:
    controller = CliBatchController(QueueStore())
    controller.bind_existing_batch(batch_id)
    return await supervise_batch(controller, batch_id)
