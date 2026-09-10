"""Bounded, redacted projections; original evidence is never a model instruction."""

from __future__ import annotations

import base64
import copy
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET = re.compile(r"authorization|cookie|password|passwd|secret|token|api[-_]?key|session|credential", re.I)
MASK = "[redacted]"


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = urlencode(
            [(k, MASK if SECRET.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    except ValueError:
        return "[invalid URL]"


def _redact_json(value):
    if isinstance(value, dict):
        return {k: MASK if SECRET.search(k) else _redact_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json(v) for v in value]
    return value


def redact_text(value: str, content_type: str = "") -> str:
    try:
        return json.dumps(_redact_json(json.loads(value)), ensure_ascii=False, indent=2)
    except (ValueError, TypeError):
        if "application/x-www-form-urlencoded" in content_type:
            return urlencode(
                [(k, MASK if SECRET.search(k) else v) for k, v in parse_qsl(value, keep_blank_values=True)]
            )
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer " + MASK, value)
        value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", MASK, value)
        return value


def public_flow(flow: dict, reveal: bool = False) -> dict:
    result = copy.deepcopy(flow)
    if not reveal:
        result["url"] = redact_url(str(result.get("url", "")))
        if result.get("path"):
            result["path"] = redact_url(str(result["path"]))
    for part in ("request", "response"):
        message = result.get(part)
        if not isinstance(message, dict):
            continue
        headers = message.get("headers") or []
        if isinstance(headers, dict):
            headers = list(headers.items())
        content_type = next((str(v) for k, v in headers if str(k).lower() == "content-type"), "")
        message["headers"] = [
            [str(k), str(v) if reveal or not SECRET.search(str(k)) else MASK] for k, v in headers
        ]
        if message.get("url") and not reveal:
            message["url"] = redact_url(str(message["url"]))
        if message.get("path") and not reveal:
            message["path"] = redact_url(str(message["path"]))
        raw = message.get("body_base64", "")
        try:
            data = base64.b64decode(raw, validate=True) if raw else b""
            text = data.decode("utf-8")
            message["body_text"] = text if reveal else redact_text(text, content_type)
            message["binary"] = False
        except (ValueError, UnicodeError):
            message["body_text"] = "[binary body]"
            message["binary"] = True
        if not reveal:
            message.pop("body_base64", None)
    return result


def public_session(session: dict | None) -> dict | None:
    if not session:
        return None
    private = {"directory", "owner_token", "journal_offset", "ca_directory"}
    return {k: v for k, v in session.items() if k not in private}
