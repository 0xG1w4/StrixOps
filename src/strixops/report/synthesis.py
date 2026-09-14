"""LLM synthesis of the client-facing executive report.

Ports the reference platform's dedicated report worker: when a run finishes,
complete finding fields, referenced evidence and supporting run context are
selected within the model's input budget and sent to a fresh model call. The
model composes the deliverable Markdown under the platform's editorial rules.

The deterministic composer in :mod:`strixops.report.state` preserves a labeled
draft for dry runs, model failures and timeouts. Only successful model synthesis
produces the final report. The synthesis call mirrors ``report.dedupe``: one
``model.get_response`` through the run's tracked model route, with a
shared total timeout and a retry without supplemental context.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agents import ModelSettings, ModelTracing

from strixops.config.context import ContextSettings
from strixops.engine.context_budget import context_window, count_tokens, output_limit
from strixops.engine.resilience import MODEL_RETRY
from strixops.engine.targets import normalize_targets
from strixops.platform import artifacts
from strixops.report.dedupe import _extract_text
from strixops.report.formatting import format_report_markdown, report_format_guidance
from strixops.report.prompt import synthesis_system_prompt as synthesis_system_prompt
from strixops.report.source import build_report_source as build_report_source
from strixops.report.state import RunState

if TYPE_CHECKING:
    from agents import Model

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 900.0
_USER_PREFIX = "Compose the final penetration-test report from the following REPORT SOURCE.\n\n"


def synthesis_enabled() -> bool:
    """Kill switch for the synthesis pass — ``STRIXOPS_REPORT_SYNTHESIS=0``
    keeps only the saved draft (fixture lifecycles, incidents)."""
    return (os.environ.get("STRIXOPS_REPORT_SYNTHESIS") or "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


_BR_SUFFIX = re.compile(r"\s*<br\s*/?\s*>\s*$", re.IGNORECASE)
_TARGET_HEADER = re.compile(r"^(?:[-*]\s+)?(?:targets?|目[标標])(?:\s*[（(][^）)]*[）)])?\s*[:：]", re.I)
_HEADER_LIST_ITEM = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")


def normalize_report_header(
    content: str, *, targets: list[str] | None = None, language: str = "zh-CN"
) -> str:
    """Guarantee one header field per paragraph in the platform-format header.

    The console renders the report with react-markdown (no ``breaks``
    option), so consecutive single-newline lines soft-wrap into one run-on
    paragraph. The reference platform solved this with ``<br>`` suffixes;
    this renderer does not render raw HTML at all, so a model imitating the
    old format loses the breaks entirely. Rebuild the block before the first
    ``---`` with a blank line between every field — mirroring the
    deterministic composer — and leave the report body untouched.
    Multi-target reports replace the model's scope field with the complete
    trusted list, including targets absent from its findings or narrative.
    """
    lines = content.splitlines()
    index = next((i for i, line in enumerate(lines) if i > 0 and line.strip() == "---"), None)
    multi = targets is not None and len(targets) > 1
    if index is None:
        if not multi:
            return content
        # A usable model report may omit the separator. Keep its body intact
        # and add the trusted scope to the title/header before the first section.
        index = next((i for i, line in enumerate(lines) if i > 0 and line.startswith("#")), 1)
    header = [_BR_SUFFIX.sub("", item) for item in lines[:index] if item.strip()]
    if not header:
        return content
    if multi:
        retained = []
        target_list = False
        for item in header:
            if _TARGET_HEADER.match(item.replace("**", "").strip()):
                target_list = True
                continue
            if target_list and _HEADER_LIST_ITEM.match(item.strip()):
                continue
            target_list = False
            retained.append(item)
        label = "目标" if language.startswith("zh") else "Targets"
        scope = f"{label} ({len(targets)}):\n\n" + "\n".join(f"- {target}" for target in targets)
        retained.insert(1 if retained and retained[0].startswith("#") else 0, scope)
        header = retained
    rebuilt: list[str] = []
    for item in header:
        if rebuilt:
            rebuilt.append("")
        rebuilt.append(item)
    if multi:
        rebuilt.append("")
    rebuilt.extend(lines[index:])
    return "\n".join(rebuilt) + "\n"


def _synthesis_timeout() -> float:
    """Total model-call budget, including a possible findings-only retry."""
    raw = os.environ.get("STRIXOPS_REPORT_SYNTHESIS_TIMEOUT") or ""
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT
    return value if math.isfinite(value) and value > 0 else _DEFAULT_TIMEOUT


async def synthesize_executive_report(
    run_state: RunState,
    resolve_model: Callable[[], Model],
) -> str | None:
    """Compose the final report via one model call over the full corpus.

    Returns the final report markdown, or ``None`` when synthesis is unavailable;
    the caller retains the saved draft without claiming a final report. Two attempts:
    full source, then a retry without supplemental context. Both preserve
    complete included finding values and share one total time budget.
    """
    loop = asyncio.get_running_loop()
    timeout = _synthesis_timeout()
    deadline = loop.time() + timeout
    language = run_state.report_language()
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    system = synthesis_system_prompt(
        language=language, format_guidance=report_format_guidance(run_state.run_dir)
    )
    model = resolve_model()
    config = run_state.run_record.get("scan_config") or {}
    model_name = str(config.get("model") or getattr(model, "model", "") or "")
    count = (
        (lambda text: count_tokens(model_name, text))
        if model_name else (lambda text: len(text.encode("utf-8")))
    )
    capacity = context_window(model_name) if model_name else ContextSettings().fallback_context_tokens
    output_reserve = output_limit(model_name) if model_name else 8192
    source_budget = capacity - output_reserve - count(system + _USER_PREFIX) - 1024
    if source_budget <= 0:
        logger.warning("No report source budget remains after instructions and output reserve")
        return None

    full_source = build_report_source(
        run_state, generated_at=generated_at, token_budget=source_budget, token_count=count,
    )
    for attempt, trimmed in enumerate((False, True), start=1):
        if loop.time() >= deadline:
            logger.warning("report synthesis exhausted its %.0fs total budget", timeout)
            break
        if trimmed:
            source = build_report_source(
                run_state, generated_at=generated_at, trimmed=True,
                token_budget=source_budget, token_count=count,
            )
            if source == full_source:
                # Repacking can replace context with complete finding fields,
                # so character length is not a reliable measure of the change.
                break
        else:
            source = full_source
        # Save the exact source prepared for this attempt for report review.
        # A diagnostic write failure cannot discard otherwise usable findings.
        try:
            artifacts.atomic_write_text(
                run_state.run_dir / ".state" / f"report-source-{attempt}.md", source
            )
            if attempt == 1:
                artifacts.atomic_write_text(run_state.run_dir / ".state" / "report-system-prompt.md", system)
        except OSError:
            logger.warning("Could not save report source snapshot for attempt %d", attempt)
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning("report synthesis exhausted its %.0fs total budget", timeout)
            break
        logger.info(
            "report synthesis attempt %d starting with %.1fs remaining in total budget",
            attempt, remaining,
        )
        try:
            response = await asyncio.wait_for(
                model.get_response(
                    system_instructions=system,
                    input=_USER_PREFIX + source,
                    model_settings=ModelSettings(
                        retry=MODEL_RETRY,
                        include_usage=True,
                        extra_args={"timeout": remaining},
                    ),
                    tools=[],
                    output_schema=None,
                    handoffs=[],
                    tracing=ModelTracing.DISABLED,
                    previous_response_id=None,
                    conversation_id=None,
                    prompt=None,
                ),
                timeout=remaining,
            )
        except TimeoutError:
            logger.warning("report synthesis attempt %d timed out", attempt)
            continue
        except Exception:
            logger.exception("report synthesis attempt %d failed", attempt)
            continue

        content = _extract_text(response).strip()
        if content.startswith("#") and len(content) > 500:
            scan_config = run_state.run_record.get("scan_config") or {}
            targets = (
                normalize_targets(scan_config.get("target", ""), scan_config["targets"])
                if "targets" in scan_config else None
            )
            report = format_report_markdown(
                normalize_report_header(content, targets=targets, language=language)
            )
            logger.info("report synthesized on attempt %d (%d chars)", attempt, len(report))
            return report
        logger.warning(
            "report synthesis attempt %d produced unusable output (%d chars)", attempt, len(content)
        )

    return None
