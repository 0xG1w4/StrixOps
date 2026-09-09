"""Scan specification: target typing, instruction ingestion, root task build.

The platform's argv maps here 1:1 (``supervisor._build_strix_argv``):
``-t <target> --scan-type web|internal --crypto --instruction-file <path>
[--socks5 V | --gsocket V]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from strixops.engine.targets import normalize_targets

if TYPE_CHECKING:
    from collections.abc import Callable

    from agents.memory import Session

    from strixops.config.context import ContextSettings

SCAN_WEB = "web"
SCAN_INTERNAL = "internal"


@dataclass
class ScanSpec:
    target: str
    scan_type: str = SCAN_WEB
    crypto: bool = False
    instruction_file: str = ""
    socks5_proxy: str = ""
    gsocket_key: str = ""
    instruction_text: str = ""
    report_language: str = "zh-CN"
    targets: list[str] = field(default_factory=list)

    def all_targets(self) -> list[str]:
        """Return the complete ordered scope, including legacy single-target specs."""
        return normalize_targets(self.target, self.targets)

    def validate(self) -> list[str]:
        problems: list[str] = []
        try:
            self.all_targets()
        except ValueError as exc:
            problems.append(str(exc))
        if self.scan_type not in (SCAN_WEB, SCAN_INTERNAL):
            problems.append(f"unknown --scan-type {self.scan_type!r}")
        if self.socks5_proxy and self.gsocket_key:
            problems.append("--socks5 and --gsocket are mutually exclusive")
        return problems

    def load_instruction(self) -> None:
        if not self.instruction_file:
            return
        path = Path(self.instruction_file)
        if path.is_file():
            self.instruction_text = path.read_text(encoding="utf-8", errors="replace")

    def language_instruction(self) -> str:
        """Reporting-language directive injected into every agent's prompt."""
        if self.report_language.startswith("zh"):
            return (
                "REPORTING LANGUAGE: 简体中文 (Simplified Chinese). Every finding report field, "
                "internal finding, and finish_scan narrative field MUST be written in 简体中文. "
                "Tool invocations, code, commands and technical identifiers stay as-is."
            )
        return (
            "REPORTING LANGUAGE: English. Every finding report field, internal finding, and "
            "finish_scan narrative field MUST be written in English."
        )

    def as_scan_config(self) -> dict:
        targets = self.all_targets()
        cfg: dict = {
            "target": targets[0],
            "scan_type": self.scan_type,
            "crypto_mode": self.crypto,
            "report_language": self.report_language,
        }
        if len(targets) > 1:
            cfg["targets"] = targets
            cfg["target_count"] = len(targets)
        if self.instruction_file:
            cfg["instruction_file"] = self.instruction_file
        if self.socks5_proxy:
            cfg["socks5_proxy"] = self.socks5_proxy
        if self.gsocket_key:
            cfg["gsocket_key"] = self.gsocket_key
        return cfg


def target_type(target: str, scan_type: str) -> str:
    """Coarse target typing mirroring the reference taxonomy."""
    if scan_type == SCAN_INTERNAL:
        return "ip_address"
    return "web_application"


def authorized_target_line(spec: ScanSpec) -> str:
    ttype = target_type(spec.target, spec.scan_type)
    return "\n".join(f"- {ttype}: {target}" for target in spec.all_targets())


def multi_target_instruction(spec: ScanSpec) -> str:
    if len(spec.all_targets()) < 2:
        return ""
    return (
        "MULTI-TARGET COORDINATION\n"
        "This is one assessment with a shared agent team and report for all listed targets. "
        "Build an asset map containing every target and any evidenced relationships or shared components. "
        "Keep coverage separate for each target; record its tested areas, blockers and untested areas. "
        "Divide work by target and purpose within the existing agent limits, and share relevant evidence "
        "to avoid duplicate work. A result on one target does not establish the same result on another. "
        "Attribute each finding and evidence item to its affected target(s), and validate cross-target "
        "relationships before relying on them. Only test relationships whose endpoints are in the "
        "listed scope. Before completion, account for every target in the final report, including "
        "targets that were unreachable or remained untested."
    )


@dataclass
class EngineServices:
    """Engine facilities shared by every agent context (injected by the runner).

    Kept as one object so tools can reach the coordinator, event stream, run
    state, child-spawn and per-agent model factory without globals.
    """

    coordinator: object | None = None
    events: object | None = None
    run_state: object | None = None
    spec: ScanSpec | None = None
    spawn_child: object | None = None  # callable(**kwargs) -> dict
    model_for: object | None = None  # callable(agent_name: str) -> model
    sandbox: object | None = None  # runtime.sandbox.SandboxBundle when live
    usage_sink: object | None = None  # record_turn(agent_id) retries pending usage writes
    root_input: str = ""
    session_for: Callable[[str], Session] | None = None
    context_settings: ContextSettings | None = None


@dataclass(frozen=True)
class LifecycleCompletion:
    """Trusted completion written by a lifecycle tool, never model output."""

    tool_name: str
    payload: dict


@dataclass
class EngineContext:
    """Per-agent context object handed to every function tool via the SDK."""

    agent_id: str = "root"
    agent_name: str = "root agent"
    parent_id: str | None = None
    run_state: object | None = None
    registry: object | None = None
    services: EngineServices | None = None
    todos: list[dict] = field(default_factory=list)
    failure_reason: str = ""
    lifecycle_completion: LifecycleCompletion | None = field(default=None, init=False, repr=False)


def build_root_task(spec: ScanSpec) -> str:
    multi = len(spec.all_targets()) > 1
    parts = [
        "You are commencing an authorized penetration test.",
        "",
        "SYSTEM-VERIFIED SCOPE — the only targets you may test:"
        if multi
        else "SYSTEM-VERIFIED SCOPE — the only target you may test:",
        authorized_target_line(spec),
        "",
    ]
    if spec.scan_type == SCAN_INTERNAL:
        parts.append("Mode: INTERNAL network penetration test.")
        if spec.socks5_proxy:
            parts.append(f"Network access is provided through a SOCKS5 tunnel: {spec.socks5_proxy}")
        if spec.gsocket_key:
            parts.append(f"Network access is provided through a GSocket tunnel with key: {spec.gsocket_key}")
        if spec.crypto:
            parts.append("CRYPTO MODE: hunt for cryptocurrency-related assets, wallets, keys, and exposure.")
    else:
        parts.append("Mode: WEB application penetration test.")
    if multi:
        parts += ["", multi_target_instruction(spec)]
    if spec.instruction_text:
        parts += ["", "OPERATOR INSTRUCTIONS (follow precisely):", spec.instruction_text]
    parts += [
        "",
        "Work autonomously. Report each validated finding via create_vulnerability_report"
        + (" or create_internal_finding for internal findings" if spec.scan_type == SCAN_INTERNAL else "")
        + ". Terminate the scan only by calling finish_scan.",
    ]
    return "\n".join(parts)
