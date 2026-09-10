"""Bounded, passive mitmproxy journal writer for the independent MCP workspace.

This file and scope.py are mounted read-only into the capture container. No
platform imports or agent execution happen in the proxy process.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from .scope import allowed_url
except ImportError:  # mitmdump loads this file directly, outside the package.
    from scope import allowed_url


BODY_LIMIT = 256 * 1024
JOURNAL_LIMIT = 128 * 1024 * 1024


def iso_timestamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(value if value is not None else time.time(), UTC).isoformat()


def classify(url: str, content_type: str) -> str:
    media = content_type.lower().split(";", 1)[0]
    if media in {"text/html", "application/xhtml+xml"}:
        return "page"
    if "json" in media or "xml" in media or "/api/" in url or "/graphql" in url:
        return "api"
    if media.startswith(("image/", "font/", "audio/", "video/")) or media in {
        "text/css",
        "application/javascript",
        "text/javascript",
        "application/wasm",
    }:
        return "asset"
    return "other"


def header_pairs(headers: Any) -> list[list[str]]:
    """Use byte fields, preserving duplicate headers and their original bytes."""
    return [[name.decode("latin-1"), value.decode("latin-1")] for name, value in headers.fields]


class CaptureAddon:
    def __init__(
        self,
        directory: str | Path | None = None,
        session_id: str | None = None,
        *,
        body_limit: int = BODY_LIMIT,
        journal_limit: int = JOURNAL_LIMIT,
    ) -> None:
        self.directory = Path(directory or os.environ.get("MCP_CAPTURE_DIR", "/capture"))
        self.session_id = session_id or os.environ.get("MCP_CAPTURE_SESSION", "")
        self.body_limit = body_limit
        self.journal_limit = journal_limit
        self.capacity_reached = False

    def _scope(self) -> dict:
        try:
            path = self.directory / "scope.json"
            if path.stat().st_size > 65536:
                raise ValueError("scope file too large")
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError("invalid scope")
            return value
        except (OSError, ValueError, TypeError):
            # A missing or malformed policy never enables broad capture.
            return {"allow_hosts": [], "exclude_hosts": [], "scope_revision": 0}

    @staticmethod
    def _allowed(url: str, scope: dict) -> bool:
        try:
            return allowed_url(url, scope.get("allow_hosts", []), scope.get("exclude_hosts", []))
        except (ValueError, TypeError):
            return False

    def tls_clienthello(self, data: Any) -> None:
        """Outside-scope HTTPS is relayed without decrypting TLS contents."""
        address = data.context.server.address
        hostname = data.client_hello.sni or (address[0] if address else "")
        port = address[1] if address else 443
        host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        scope = self._scope()
        if not self._allowed(f"https://{host}:{port}/", scope):
            data.ignore_connection = True

    def _status(self, state: str, message: str = "") -> None:
        try:
            temporary = self.directory / ".capture-status.tmp"
            temporary.write_text(
                json.dumps({"state": state, "message": message, "updated_at": iso_timestamp()})
            )
            temporary.chmod(0o600)
            temporary.replace(self.directory / "capture-status.json")
        except OSError:
            pass

    def running(self) -> None:
        self._status("capturing")

    def _prepare(self, flow: Any) -> dict:
        if "mcp_capture" not in flow.metadata:
            scope = self._scope()
            flow.metadata["mcp_capture"] = {
                "allowed": self._allowed(flow.request.url, scope),
                "scope_revision": scope.get("scope_revision", 0),
                "request": {"body": bytearray(), "size": 0, "complete": False},
                "response": {"body": bytearray(), "size": 0, "complete": False},
                "last_emit": 0.0,
            }
        return flow.metadata["mcp_capture"]

    def _stream(self, flow: Any, side: str):
        state = self._prepare(flow)

        def receive(chunk: bytes) -> bytes:
            part = state[side]
            part["size"] += len(chunk)
            remaining = self.body_limit - len(part["body"])
            if remaining > 0:
                part["body"].extend(chunk[:remaining])
            if not chunk:
                part["complete"] = True
            # SSE/long downloads are visible before the connection closes.
            if side == "response" and time.monotonic() - state["last_emit"] >= 1:
                self._emit(flow, "streaming")
            return chunk  # Never modify or queue the browser's traffic.

        return receive

    def requestheaders(self, flow: Any) -> None:
        state = self._prepare(flow)
        flow.request.stream = self._stream(flow, "request") if state["allowed"] else True
        self._emit(flow, "request_headers")

    def request(self, flow: Any) -> None:
        self._prepare(flow)["request"]["complete"] = True
        self._emit(flow, "request")

    def responseheaders(self, flow: Any) -> None:
        state = self._prepare(flow)
        flow.response.stream = self._stream(flow, "response") if state["allowed"] else True
        self._emit(flow, "response_headers")

    def response(self, flow: Any) -> None:
        self._prepare(flow)["response"]["complete"] = True
        self._emit(flow, "complete")

    def error(self, flow: Any) -> None:
        self._emit(flow, "error")

    def _message(self, message: Any, body: dict, *, request: bool = False) -> dict | None:
        if message is None:
            return None
        raw = message.raw_content
        # raw_content is absent during streaming; the bounded callback buffer
        # contains compressed/entity bytes exactly as received by the proxy.
        content = bytes(body["body"]) if raw is None else raw[: self.body_limit]
        size = body["size"] if raw is None else len(raw)
        result = {
            "headers": header_pairs(message.headers),
            "body_base64": base64.b64encode(content).decode("ascii"),
            "body_size": size,
            "truncated": size > self.body_limit,
            "body_complete": body["complete"],
            "http_version": message.http_version,
        }
        if request:
            result.update(method=message.method, url=message.url)
        else:
            result["status_code"] = message.status_code
        return result

    def _emit(self, flow: Any, stage: str) -> None:
        state = self._prepare(flow)
        if not state["allowed"] or self.capacity_reached:
            return
        request = self._message(flow.request, state["request"], request=True)
        response = self._message(flow.response, state["response"])
        media = flow.response.headers.get("content-type", "") if flow.response else ""
        started = flow.request.timestamp_start
        record = {
            "id": flow.id,
            "session_id": self.session_id,
            "source": "user",
            "created_at": iso_timestamp(started),
            "updated_at": iso_timestamp(),
            "stage": stage,
            "method": flow.request.method,
            "url": flow.request.url,
            "host": flow.request.host,
            "path": flow.request.path,
            "status_code": flow.response.status_code if flow.response else None,
            "content_type": media,
            "duration_ms": max(0, round((time.time() - started) * 1000)),
            "kind": classify(flow.request.url, media),
            "request": request,
            "response": response,
            "error": str(flow.error.msg)[:1000] if flow.error else None,
            "truncated": bool(request["truncated"] or (response and response["truncated"])),
            "scope_revision": state["scope_revision"],
        }
        encoded = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        try:
            path = self.directory / "events.jsonl"
            size = path.stat().st_size if path.exists() else 0
            if size + len(encoded) > self.journal_limit:
                self.capacity_reached = True
                self._status("capacity_reached", "Capture journal limit reached; forwarding continues.")
                return
            # One proxy event loop owns this append-only file; readers only read
            # complete newline-terminated records and can restart by byte offset.
            with path.open("ab") as output:
                output.write(encoded)
                output.flush()
            path.chmod(0o600)
            state["last_emit"] = time.monotonic()
        except OSError:
            self.capacity_reached = True
            self._status("storage_error", "Cannot persist capture journal; forwarding continues.")


addons = [CaptureAddon()]
