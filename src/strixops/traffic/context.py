"""Bounded, already-redacted evidence for the request Agent's first model call.

The projection callback is the same security boundary used by inspect_request.
This helper never recovers omitted fields from raw traffic. A preview counts as
inspected only when the entire projected baseline is supplied and usable; a
shortened or incomplete baseline still requires an explicit inspection.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from typing import Any

MAX_CONTEXT_BYTES = 24 * 1024
MAX_BODY_BYTES = 4096
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_RAW_FIELDS = {"body_base64", "raw", "raw_request", "raw_response"}
_IMPORTANT_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "content-type",
    "content-encoding",
    "content-length",
    "location",
    "www-authenticate",
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "content-security-policy",
    "x-frame-options",
}
_MESSAGE_FIELDS = (
    "method",
    "url",
    "status_code",
    "http_version",
    "body_size",
    "body_complete",
    "truncated",
    "binary",
    "body_decode_error",
    "body_decoded",
    "body_encoding",
    "body_charset",
    "body_preview_truncated",
)


def _encoded(value: Any) -> bytes:
    # Match the Agent's ordinary ensure_ascii=False JSON serialization, including
    # spaces. A character bound alone would underestimate non-ASCII evidence.
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _clip(value: str, limit: int) -> str:
    if len(value.encode("utf-8")) <= limit:
        return value
    text = value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    # Keep a mask whole if a byte limit lands in its middle.
    start = text.rfind("[")
    if start >= 0 and "[redacted]".startswith(text[start:]):
        text = text[:start]
    return text


def _check_projection(value: Any, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("Selected request projection exceeds the nesting limit")
    if isinstance(value, dict):
        if _RAW_FIELDS.intersection(value):
            raise ValueError("Selected request projection contains raw evidence")
        for item in value.values():
            _check_projection(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_projection(item, depth + 1)


def _limitations(flow: dict) -> list[str]:
    issues = []
    for side in ("request", "response"):
        message = flow.get(side)
        if not isinstance(message, dict):
            issues.append(f"{side}_missing")
            continue
        if message.get("binary"):
            issues.append(f"{side}_binary")
        if message.get("body_complete") is False or message.get("truncated"):
            issues.append(f"{side}_incomplete")
        if message.get("body_preview_truncated") or "[truncated]" in str(message.get("body_text", "")):
            issues.append(f"{side}_preview_truncated")
        if message.get("body_decode_error"):
            issues.append(f"{side}_decode_error")
    if flow.get("error"):
        issues.append("request_error")
    return issues


def _metadata(flow: dict, text_limit: int) -> dict:
    return {
        "id": flow["id"],
        "method": _clip(flow["method"], min(32, text_limit)),
        "url": _clip(flow["url"], text_limit),
        "status_code": flow["status_code"],
        "content_type": _clip(flow["content_type"], min(128, text_limit)),
        "context_preview_truncated": True,
        "inspect_required": True,
    }


def _message_preview(message: dict, limit: int) -> dict:
    preview = {
        key: _clip(value, limit) if isinstance(value, str) else value
        for key in _MESSAGE_FIELDS
        if (value := message.get(key)) is not None
    }
    headers = [pair for pair in message.get("headers", []) if isinstance(pair, list) and len(pair) == 2]
    # Prefer useful control/security headers in shortened previews; full
    # projections preserve every projected header and its original ordering.
    headers.sort(key=lambda pair: str(pair[0]).lower() not in _IMPORTANT_HEADERS)
    preview["headers"] = [[_clip(str(k), 100), _clip(str(v), limit)] for k, v in headers[:16]]
    if isinstance(message.get("body_text"), str):
        preview["body_text"] = _clip(message["body_text"], min(MAX_BODY_BYTES, limit * 2))
        if preview["body_text"] != message["body_text"]:
            preview["body_preview_truncated"] = True
    return preview


def _bounded_flow(flow: dict, quota: int) -> tuple[dict, bool]:
    issues = _limitations(flow)
    complete = copy.deepcopy(flow)
    shortened = False
    for side in ("request", "response"):
        message = complete.get(side)
        if isinstance(message, dict) and isinstance(message.get("body_text"), str):
            text = _clip(message["body_text"], MAX_BODY_BYTES)
            if text != message["body_text"]:
                message.update(body_text=text, body_preview_truncated=True)
                shortened = True
    complete.update(context_preview_truncated=shortened, inspect_required=bool(issues or shortened))
    if issues:
        complete["evidence_limitations"] = issues
    if len(_encoded(complete)) <= quota:
        return complete, not complete["inspect_required"]

    for limit in (2048, 1024, 512, 256, 128, 64, 32):
        row = _metadata(flow, limit)
        if issues:
            row["evidence_limitations"] = issues
        for side in ("request", "response"):
            if isinstance(flow.get(side), dict):
                row[side] = _message_preview(flow[side], limit)
        if len(_encoded(row)) <= quota:
            return row, False

    # Large selections get a fair metadata allocation for every flow. Details
    # omitted here remain available through inspect_request; no first-N bias.
    for limit in (256, 128, 64, 32, 16, 8, 0):
        row = _metadata(flow, limit)
        if issues:
            row["evidence_limitations"] = issues
        if len(_encoded(row)) <= quota:
            return row, False
    raise ValueError("Selected request metadata exceeds the initial context limit")


def prepare_initial_context(flows: list[dict], project: Callable[[dict], dict]) -> dict:
    """Return at most 24 KiB of JSON evidence for 1–50 selected requests.

    ``omitted_flow_ids`` identifies baselines not fully supplied/usable, including
    shortened previews, incomplete captures and binary bodies. All selected IDs
    still appear in ``selected_requests``. Only ``preinspected_flow_ids`` may seed
    the Agent's inspection bookkeeping; this never implies vulnerability coverage.
    Projected identifiers must remain exact, never truncated or reconstructed from
    redacted text. Projection failure is fatal, with no raw-evidence fallback.
    """
    if not 1 <= len(flows) <= 50:
        raise ValueError("Select between 1 and 50 requests for initial context")
    projected, identities = [], []
    for raw in flows:
        identity = raw.get("id") or raw.get("flow_id")
        if not isinstance(identity, str) or not _IDENTIFIER.fullmatch(identity) or identity in identities:
            raise ValueError("Selected requests require distinct stable flow identifiers")
        try:
            value = project(copy.deepcopy(raw))
            if not isinstance(value, dict) or (value.get("id") or value.get("flow_id")) != identity:
                raise ValueError("Selected request projection changed its flow identifier")
            _check_projection(value)
            value = json.loads(_encoded(value))
            request = value.get("request") or {}
            response = value.get("response") or {}
            value["id"] = identity
            value["method"] = str(value.get("method") or request.get("method") or "")
            value["url"] = str(value.get("url") or request.get("url") or "")
            value["status_code"] = value.get("status_code", response.get("status_code"))
            value["content_type"] = str(
                value.get("content_type")
                or next((v for k, v in response.get("headers", []) if str(k).lower() == "content-type"), "")
            )
        except Exception:
            # The callback or malformed projection can carry captured values in
            # its exception text. Neither return raw traffic nor expose that text.
            raise ValueError("Selected request evidence could not be safely prepared") from None
        identities.append(identity)
        projected.append(value)

    result = {
        "selected_requests": [],
        "preinspected_flow_ids": [],
        "omitted_flow_ids": identities.copy(),
        "omitted_count": len(identities),
        "limits": {"max_bytes": MAX_CONTEXT_BYTES, "body_preview_bytes": MAX_BODY_BYTES},
    }
    # Each ID appears in exactly one of the two bookkeeping lists. Reserve list
    # separators and a little envelope slack before assigning equal row quotas.
    quota = (MAX_CONTEXT_BYTES - len(_encoded(result)) - 2 * len(flows) - 32) // len(flows)
    inspected = []
    for flow in projected:
        row, usable = _bounded_flow(flow, quota)
        result["selected_requests"].append(row)
        if usable:
            inspected.append(flow["id"])
    result["preinspected_flow_ids"] = inspected
    result["omitted_flow_ids"] = [identity for identity in identities if identity not in inspected]
    result["omitted_count"] = len(result["omitted_flow_ids"])
    if len(_encoded(result)) > MAX_CONTEXT_BYTES:
        raise ValueError("Selected request context exceeds its byte limit")
    return result
