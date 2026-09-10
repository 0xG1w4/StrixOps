"""Bounded text previews of captured HTTP bytes; original wire evidence stays untouched."""

from __future__ import annotations

import base64
import codecs
import re
import zlib
from typing import Any

RAW_LIMIT = 1024 * 1024
PREVIEW_LIMIT = 256 * 1024
_CHARSET = re.compile(r"""(?i)(?:^|;)\s*charset\s*=\s*(?:"([^"]*)"|'([^']*)'|([^;\s]*))""")
_TEXT_MEDIA = {
    "application/json",
    "application/javascript",
    "application/x-javascript",
    "application/xml",
    "application/x-www-form-urlencoded",
    "application/graphql",
    "application/sql",
}


def _header(message: dict, name: str) -> str:
    headers = message.get("headers") or []
    pairs = headers.items() if isinstance(headers, dict) else headers
    return ", ".join(
        str(pair[1])
        for pair in pairs
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and str(pair[0]).lower() == name
    )[:1024]


def _inflate(data: bytes, encoding: str) -> tuple[bytes, bool, str | None]:
    """Limit decompressor output before allocation, including concatenated gzip members."""
    windows = (zlib.MAX_WBITS | 16,) if encoding in {"gzip", "x-gzip"} else (zlib.MAX_WBITS, -zlib.MAX_WBITS)
    for window in windows:
        output = bytearray()
        pending = data
        try:
            while True:
                decoder = zlib.decompressobj(window)
                output.extend(decoder.decompress(pending, PREVIEW_LIMIT + 1 - len(output)))
                if len(output) > PREVIEW_LIMIT:
                    return bytes(output[:PREVIEW_LIMIT]), True, None
                if not decoder.eof:
                    return bytes(output), True, "incomplete_body"
                pending = decoder.unused_data
                if not pending:
                    return bytes(output), False, None
                if encoding not in {"gzip", "x-gzip"}:
                    return b"", False, "invalid_compression"
        except zlib.error:
            continue
    return b"", False, "invalid_compression"


def _brotli(data: bytes) -> tuple[bytes, bool, str | None]:
    try:
        import brotli
    except ImportError:
        return b"", False, "unsupported_encoding"
    try:
        decoder = brotli.Decompressor()
        # Brotli >= 1.2.0 bounds the native output buffer. Never fall back to the
        # unbounded convenience decompress() API on an older installed version.
        output = decoder.process(data, output_buffer_limit=PREVIEW_LIMIT + 1)
        if len(output) > PREVIEW_LIMIT or not decoder.can_accept_more_data():
            return output[:PREVIEW_LIMIT], True, None
        return output, not decoder.is_finished(), None if decoder.is_finished() else "incomplete_body"
    except (TypeError, AttributeError):
        return b"", False, "unsupported_encoding"
    except brotli.error:
        return b"", False, "invalid_compression"


def _charset(content_type: str, data: bytes) -> tuple[str, str | None]:
    match = _CHARSET.search(content_type)
    name = next((item for item in match.groups() if item is not None), "") if match else ""
    if not name:
        name = (
            "utf-8-sig"
            if data.startswith(codecs.BOM_UTF8)
            else ("utf-16" if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8")
        )
    try:
        codec = codecs.lookup(name)
        if (
            not codec._is_text_encoding
            or codec.incrementaldecoder is None
            or codec.name in {"idna", "undefined", "punycode", "unicode-escape", "raw-unicode-escape"}
        ):
            raise LookupError
        return codec.name, None
    except (LookupError, ValueError, UnicodeError):
        return "utf-8", "invalid_charset"


def decode_body(message: dict) -> dict[str, Any]:
    """Return a bounded display/model projection without changing message bytes or headers.

    Error codes distinguish failed decoding from genuine binary content. A partial
    textual prefix is useful evidence, but never claims a complete response body.
    """
    content_type = _header(message, "content-type")
    media = content_type.split(";", 1)[0].strip().lower()
    encoding = _header(message, "content-encoding").lower().strip()
    preview = {
        "body_text": "",
        "binary": False,
        "body_decoded": False,
        "body_encoding": encoding,
        "body_charset": "",
        "body_preview_truncated": bool(message.get("truncated") or message.get("body_complete") is False),
        "body_decode_error": None,
    }
    encoded = message.get("body_base64") or ""
    if not isinstance(encoded, str):
        preview["body_decode_error"] = "invalid_base64"
        return preview
    encoded_limit = ((RAW_LIMIT + 2) // 3) * 4
    if len(encoded) > encoded_limit:
        encoded = encoded[:encoded_limit]
        preview["body_preview_truncated"] = True
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError):
        preview["body_decode_error"] = "invalid_base64"
        return preview
    if len(data) > RAW_LIMIT:
        data = data[:RAW_LIMIT]
        preview["body_preview_truncated"] = True
    if not data:
        # HEAD/204/304 responses may advertise their representation's encoding
        # without carrying any entity bytes to decompress.
        if message.get("body_complete") is False:
            preview["body_decode_error"] = "incomplete_body"
        return preview
    encodings = [item.strip() for item in encoding.split(",") if item.strip() and item.strip() != "identity"]
    if len(encodings) > 3 or any(item not in {"gzip", "x-gzip", "deflate", "br"} for item in encodings):
        preview["body_decode_error"] = "unsupported_encoding"
        return preview
    for index, item in enumerate(reversed(encodings)):
        data, limited, error = _brotli(data) if item == "br" else _inflate(data, item)
        preview["body_decoded"] = error not in {"invalid_compression", "unsupported_encoding"}
        preview["body_preview_truncated"] |= limited
        if error:
            preview["body_decode_error"] = error
        if error in {"invalid_compression", "unsupported_encoding"}:
            return preview
        if limited and index < len(encodings) - 1:
            # A bounded intermediate layer may still contain encoded bytes;
            # running the next bounded decoder can recover its text prefix.
            preview["body_preview_truncated"] = True
    if len(data) > PREVIEW_LIMIT:
        data = data[:PREVIEW_LIMIT]
        preview["body_preview_truncated"] = True
    textual = media.startswith("text/") or media in _TEXT_MEDIA or media.endswith(("+json", "+xml"))
    declared_binary = media.startswith(("image/", "audio/", "video/", "font/")) or media in {
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-protobuf",
        "application/protobuf",
        "application/wasm",
    }
    if declared_binary and not textual and data:
        preview.update(binary=True, body_text="[binary body]")
        return preview
    charset, charset_error = _charset(content_type, data)
    preview["body_charset"] = charset
    if charset_error:
        preview["body_decode_error"] = preview["body_decode_error"] or charset_error
    try:
        decoder = codecs.getincrementaldecoder(charset)(errors="strict")
        text = decoder.decode(data, final=not preview["body_preview_truncated"])
    except (UnicodeError, ValueError):
        if not textual:
            preview.update(binary=True, body_text="[binary body]")
            return preview
        preview["body_decode_error"] = preview["body_decode_error"] or "invalid_text"
        try:
            text = data.decode(charset, errors="replace")
        except (UnicodeError, ValueError, LookupError):
            # Some Python transformation codecs claim to be text encodings but
            # reject replacement decoding. A hostile charset must never break
            # the flow API or cause a Latin-1 interpretation of binary bytes.
            preview["body_charset"] = "utf-8"
            preview["body_decode_error"] = "invalid_charset"
            text = data.decode("utf-8", errors="replace")
    controls = sum(ord(char) < 32 and char not in "\t\n\r\f" for char in text)
    if not textual and ("\x00" in text or controls > max(1, len(text) // 100)):
        preview.update(binary=True, body_text="[binary body]")
        return preview
    preview["body_text"] = text
    if preview["body_preview_truncated"] and not preview["body_decode_error"]:
        preview["body_decode_error"] = "incomplete_body" if message.get("body_complete") is False else None
    return preview
