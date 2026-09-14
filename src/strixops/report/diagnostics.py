"""Safe, durable report-generation outcomes without provider payloads or secrets."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from openai import APIError, APITimeoutError

from strixops.config.model_errors import upstream_policy_code
from strixops.platform import artifacts

logger = logging.getLogger(__name__)

MESSAGES = {
    "timeout": "Report generation exceeded its time limit. Retry or check the model service latency.",
    "input_budget": "The saved report source cannot fit the configured model's input capacity.",
    "source_error": "The saved report source could not be prepared. Check the task's engine log.",
    "model_unavailable": (
        "The report model could not be configured. Check the saved model profile and credentials."
    ),
    "model_error": "The model request failed. Check the model service and retry.",
    "authentication": (
        "The model service rejected authentication or access. Check the model profile credentials."
    ),
    "rate_limit": "The model service reported a rate or quota limit. Check the account and retry later.",
    "output_limit": "The model reached its output token limit before completing the report.",
    "context_limit": "The model service rejected the report source because it exceeded the context limit.",
    "upstream_policy": "The model provider rejected the request under its cybersecurity policy.",
    "model_refusal": "The model declined to generate the report.",
    "incomplete_output": "The model returned an incomplete report. The previous saved report was retained.",
    "empty_output": "The model returned no report text. Retry or check the model profile.",
    "invalid_output": (
        "The model did not return a usable Markdown report. The previous saved report was retained."
    ),
    "cancelled": "Report generation was interrupted. It can be started again after the task has stopped.",
    "disabled": "Automatic model report generation is disabled. You can generate a report manually.",
    "dry_run": "This was a dry run, so automatic model report generation was skipped.",
}


def synthesis_error_code(exc: BaseException) -> str:
    if upstream_policy_code(exc):
        return "upstream_policy"
    if isinstance(exc, (TimeoutError, APITimeoutError)):
        return "timeout"
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "authentication"
    if status == 429:
        return "rate_limit"
    # Recognize fixed machine markers; never publish the surrounding message.
    message = str(exc)[:8192].lower()
    if "content_filter" in message or "model_refusal" in message:
        return "model_refusal"
    if "max_output_tokens" in message or "max_completion_tokens" in message:
        return "output_limit"
    if "incomplete_output" in message:
        return "incomplete_output"
    if "context_length_exceeded" in message or "contextwindowexceeded" in type(exc).__name__.lower():
        return "context_limit"
    if isinstance(exc, APIError) and exc.code == "insufficient_quota":
        return "rate_limit"
    return "model_error"


def set_report_synthesis_status(
    run_state: Any, status: str, code: str = "", *, attempt: int = 0,
) -> dict[str, Any]:
    record = {
        "status": status,
        "code": code,
        "message": MESSAGES.get(code, ""),
        "attempt": attempt,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    run_state.run_record["report_synthesis"] = record
    # Do not save the complete RunState here: a retry loads immutable artifacts,
    # and must not rewrite scan state or findings as a side effect of diagnostics.
    try:
        artifacts.atomic_write_text(
            run_state.run_dir / ".state" / "report-synthesis.json",
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        )
    except OSError:
        logger.warning("Could not save report synthesis diagnostics")
    return record
