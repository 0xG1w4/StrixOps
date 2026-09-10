"""Bounded, redacted projections; original evidence is never a model instruction."""

from __future__ import annotations

import copy
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .body import PREVIEW_LIMIT, decode_body

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


def _json_value_end(text: str, start: int) -> int:
    """Find a JSON value boundary, consuming an incomplete value to the end."""
    stack: list[str] = []
    quoted = False
    escaped = False
    compound = start < len(text) and text[start] in "[{"
    string = start < len(text) and text[start] == '"'
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
                if string and not stack:
                    return index + 1
            continue
        if char == '"':
            quoted = True
        elif char in "[{":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack:
                return index
            if stack.pop() != char:
                return len(text)
            if compound and not stack:
                return index + 1
        elif char == "," and not stack:
            return index
    return len(text)


def _redact_json_prefix(text: str, *, json_like: bool = False) -> str:
    """Mask scalar or nested sensitive values even when capture ends mid-JSON."""
    json_like = json_like or text.lstrip().startswith(("{", "["))
    decoder = json.JSONDecoder()
    pieces: list[str] = []
    cursor = index = 0
    while index < len(text):
        if text[index] != '"':
            index += 1
            continue
        try:
            key, end = decoder.raw_decode(text, index)
        except (ValueError, RecursionError):
            if json_like:
                # Invalid string escapes or cut keys prevent reliable field
                # boundaries. Never return an unchecked JSON suffix containing
                # later credentials; ordinary HTML/plaintext keeps its preview.
                return "".join(pieces) + text[cursor:index] + MASK
            break
        after = end
        while after < len(text) and text[after].isspace():
            after += 1
        if after < len(text) and text[after] == ":" and SECRET.search(key):
            start = after + 1
            while start < len(text) and text[start].isspace():
                start += 1
            end = _json_value_end(text, start)
            pieces.extend((text[cursor:start], json.dumps(MASK)))
            cursor = end
        index = end
    return "".join(pieces) + text[cursor:]


def redact_text(value: str, content_type: str = "") -> str:
    try:
        chunks = json.JSONEncoder(ensure_ascii=False, indent=2).iterencode(_redact_json(json.loads(value)))
        parts: list[str] = []
        size = 0
        for chunk in chunks:
            remaining = PREVIEW_LIMIT + 1 - size
            parts.append(chunk[:remaining])
            size += len(parts[-1])
            if size > PREVIEW_LIMIT:
                break
        return "".join(parts)
    except (ValueError, TypeError, RecursionError):
        if "application/x-www-form-urlencoded" in content_type.lower():
            return urlencode(
                [(k, MASK if SECRET.search(k) else v) for k, v in parse_qsl(value, keep_blank_values=True)]
            )
        value = _redact_json_prefix(value, json_like="json" in content_type.lower())
        value = re.sub(
            r"""(?i)(["'][^"']*(?:authorization|cookie|password|passwd|secret|token|api[-_]?key|session|credential)[^"']*["']\s*:\s*)(["'])(?:\\.|(?!\2)[^\\])*?(?:\2|$)""",
            lambda match: match.group(1) + '"' + MASK + '"',
            value,
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
        preview = decode_body(message)
        if not reveal and not preview["binary"]:
            preview["body_text"] = redact_text(preview["body_text"], content_type)
            if len(preview["body_text"]) > PREVIEW_LIMIT:
                preview["body_text"] = preview["body_text"][:PREVIEW_LIMIT]
                preview["body_preview_truncated"] = True
        message.update(preview)
        if not reveal:
            message.pop("body_base64", None)
    return result


def public_session(session: dict | None) -> dict | None:
    if not session:
        return None
    private = {"directory", "owner_token", "journal_offset", "ca_directory"}
    return {k: v for k, v in session.items() if k not in private}
