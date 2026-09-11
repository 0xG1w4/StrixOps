"""Private CLI child entry point, loading its immutable launch snapshot."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
from pathlib import Path

from strixops.cli import _run_with_signal_handlers
from strixops.config.settings import EngineSettings
from strixops.engine.scanconfig import ScanSpec

from .cli import CliBatchController
from .store import QueueError, QueueStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--item", required=True)
    args = parser.parse_args()
    try:
        controller = CliBatchController(QueueStore())
        item = controller.store.internal_item(args.item)
        if item["launch_token"] != os.environ.get("STRIXOPS_QUEUE_ITEM_TOKEN"):
            raise QueueError("queue_invalid")
        snapshot = controller._snapshot(item)
        run_dir = Path(snapshot["settings"]["strix_runs"]) / item["run_name"]
        spec = dataclasses.replace(
            ScanSpec(**snapshot["spec"]), target=item["target"], targets=[], instruction_file=""
        )
        settings = dataclasses.replace(
            EngineSettings(**snapshot["settings"]),
            operator_hints_dir=str(run_dir / "operator_hints"),
            host_workspace_dir=str(run_dir / "workspace"),
        )
        return asyncio.run(_run_with_signal_handlers(spec, settings))
    except QueueError as exc:
        print(str(exc), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
