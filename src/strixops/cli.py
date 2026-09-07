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

from strixops import __version__
from strixops.config.settings import EngineSettings
from strixops.engine.runner import EXIT_FAILED, run_scan
from strixops.engine.scanconfig import SCAN_INTERNAL, SCAN_WEB, ScanSpec

_INTERRUPT: dict[str, int] = {"signum": 0}


def _install_signal_handlers() -> None:
    """SIGTERM/SIGINT → KeyboardInterrupt inside asyncio.run, exit 143/130.

    asyncio.run's cleanup then cancels the main task, which lets run_scan
    record ``interrupted`` and tear the sandbox down before the process dies.
    """

    def _handler(signum: int, _frame: object) -> None:
        _INTERRUPT["signum"] = signum
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):  # not main thread / unsupported
            signal.signal(sig, _handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strix",
        description="StrixOps autonomous pentest engine (platform scan core)",
    )
    parser.add_argument("-t", "--target", help="scan target (URL / host / IP)")
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

    spec = ScanSpec(
        target=(args.target or "").strip(),
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
        _install_signal_handlers()
        return asyncio.run(run_scan(spec, settings))
    except KeyboardInterrupt:
        signum = _INTERRUPT["signum"] or int(signal.SIGINT)
        print(f"interrupted by signal {signum}", file=sys.stderr, flush=True)
        return 128 + signum  # 143 SIGTERM / 130 SIGINT


if __name__ == "__main__":
    sys.exit(main())
