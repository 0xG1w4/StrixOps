"""StrixOps CLI — the exact argv surface the platform supervisor uses.

``strix -t <target> --scan-type web|internal --crypto --instruction-file <path>
[--socks5 <val> | --gsocket <val>]``

Spawned with piped stdio (no TTY) from ``$STRIX_HOME``; never assume an
interactive terminal. Extra flags beyond the platform contract are
 tolerated but not required.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import os
import signal
import sys
from collections.abc import Coroutine
from typing import Any

from strixops import __version__
from strixops.config.settings import EngineSettings
from strixops.engine.runner import EXIT_FAILED, run_scan
from strixops.engine.scanconfig import SCAN_INTERNAL, SCAN_WEB, ScanSpec
from strixops.engine.targets import normalize_targets, read_target_list


async def _run_with_signal_handlers(
    spec: ScanSpec,
    settings: EngineSettings,
    *,
    max_concurrent: int = 2,
    batch_name: str = "",
) -> int:
    """Cancel only the scan driver; its shielded finalizer owns shared cleanup.

    Raising KeyboardInterrupt from a signal handler makes asyncio.run cancel
    every task, including the finalizer. Repeated signals therefore request
    cancellation of this same driver and never escape into loop shutdown.
    """
    if len(spec.all_targets()) > 1:
        from strixops.queue.cli import run_batch

        work = run_batch(spec, settings, max_concurrent=max_concurrent, name=batch_name)
    else:
        work = run_scan(spec, settings)
    return await _await_with_signal_handlers(work)


async def _await_with_signal_handlers(work: Coroutine[Any, Any, int]) -> int:
    loop = asyncio.get_running_loop()
    scan_task = asyncio.create_task(work, name="strixops-scan")
    received_signal = 0
    previous: dict[signal.Signals, object] = {}

    def handler(signum: int, _frame: object) -> None:
        nonlocal received_signal
        if not received_signal:
            received_signal = signum
        loop.call_soon_threadsafe(scan_task.cancel)

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):
            prior = signal.getsignal(sig)
            signal.signal(sig, handler)
            previous[sig] = prior
    try:
        try:
            result = await scan_task
        except asyncio.CancelledError:
            if not received_signal:
                raise
            result = 128 + received_signal
        if received_signal:
            print(f"interrupted by signal {received_signal}", file=sys.stderr, flush=True)
            return 128 + received_signal
        return result
    finally:
        for sig, prior in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, prior)


def _run_cli_process(work: Coroutine[Any, Any, int]) -> int:
    previous = os.environ.get("STRIXOPS_QUEUE_PROCESS_BOUNDARY")
    os.environ["STRIXOPS_QUEUE_PROCESS_BOUNDARY"] = "1"
    try:
        return asyncio.run(work)
    finally:
        if previous is None:
            os.environ.pop("STRIXOPS_QUEUE_PROCESS_BOUNDARY", None)
        else:
            os.environ["STRIXOPS_QUEUE_PROCESS_BOUNDARY"] = previous


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strix",
        description="StrixOps autonomous pentest engine (platform scan core)",
    )
    parser.add_argument(
        "-t",
        "--target",
        action="append",
        help="scan target (URL / host / IP); repeat for a batch of independent target runs",
    )
    parser.add_argument(
        "--target-list",
        action="append",
        metavar="PATH",
        help="UTF-8 target file, one per nonempty noncomment line; repeat or combine with --target",
    )
    parser.add_argument(
        "--scan-type",
        choices=[SCAN_WEB, SCAN_INTERNAL],
        default=SCAN_WEB,
        help="web application or internal network assessment",
    )
    parser.add_argument(
        "--crypto",
        action="store_true",
        help="crypto-asset hunting emphasis (internal mode)",
    )
    parser.add_argument("--instruction-file", default="", help="path to operator instruction markdown")
    parser.add_argument("--instruction", default="", help="inline instruction (fallback)")
    parser.add_argument(
        "--max-concurrent", type=int, default=2, help="maximum active targets in a batch (1-16)"
    )
    parser.add_argument("--batch-name", default="", help="optional name for a multi-target batch")
    parser.add_argument("--resume-batch", default="", help="resume supervising an existing CLI batch ID")
    parser.add_argument("--socks5", default="", help="SOCKS5 proxy for internal reachability")
    parser.add_argument("--gsocket", default="", help="GSocket key for internal reachability")
    parser.add_argument(
        "-n", "--non-interactive", action="store_true", help="headless (always the case here)"
    )
    parser.add_argument(
        "--report-language",
        choices=["zh-CN", "en"],
        default="",
        help="report/finding content language (default zh-CN; env STRIXOPS_REPORT_LANG)",
    )
    parser.add_argument("--version", action="version", version=f"StrixOps {__version__}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=argparse.SUPPRESS,  # scripted no-LLM full-stack run (testing)
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.max_concurrent <= 16:
        print("error: --max-concurrent must be between 1 and 16", file=sys.stderr, flush=True)
        return EXIT_FAILED
    if args.resume_batch:
        from strixops.queue import QueueError
        from strixops.queue.cli import resume_batch

        try:
            return _run_cli_process(_await_with_signal_handlers(resume_batch(args.resume_batch)))
        except QueueError as exc:
            print(f"error: {exc}", file=sys.stderr, flush=True)
            return EXIT_FAILED

    try:
        targets = list(args.target or [])
        for path in args.target_list or []:
            targets.extend(read_target_list(path))
        targets = normalize_targets(targets=targets)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return EXIT_FAILED

    spec = ScanSpec(
        target=targets[0],
        targets=targets if len(targets) > 1 else [],
        scan_type=args.scan_type,
        crypto=bool(args.crypto),
        instruction_file=args.instruction_file or "",
        socks5_proxy=(args.socks5 or "").strip(),
        gsocket_key=(args.gsocket or "").strip(),
        report_language=(getattr(args, "report_language", "") or "").strip()
        or (os.environ.get("STRIXOPS_REPORT_LANG") or "").strip()
        or "zh-CN",
    )
    if not spec.instruction_file and args.instruction:
        spec.instruction_text = args.instruction

    problems = spec.validate()
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr, flush=True)
        return EXIT_FAILED

    spec.load_instruction()

    settings = EngineSettings.from_env()
    if args.dry_run:
        settings = dataclasses.replace(settings, dry_run=True)

    try:
        return _run_cli_process(
            _run_with_signal_handlers(
                spec,
                settings,
                max_concurrent=args.max_concurrent,
                batch_name=args.batch_name,
            )
        )
    except KeyboardInterrupt:
        # Covers interruption before the coroutine installs its handlers.
        print(f"interrupted by signal {int(signal.SIGINT)}", file=sys.stderr, flush=True)
        return 128 + int(signal.SIGINT)


if __name__ == "__main__":
    sys.exit(main())
