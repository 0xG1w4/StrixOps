"""One structured HTTP/1.1 replay, executed in a short-lived owned container.

No shell, redirects, environment proxy, authentication refresh, or agent code.
Input/output files are the only control channel and stdout never contains traffic.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import signal
import ssl
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

try:
    from .scope import allowed_url
except ImportError:
    from scope import allowed_url


BODY_LIMIT = 256 * 1024
REQUEST_LIMIT = 1024 * 1024
TIMEOUT = 15
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HOP = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "proxy-connection",
    "proxy-authorization",
    "proxy-authenticate",
    "upgrade",
    "te",
    "trailer",
    "keep-alive",
}


def _origin(url: str) -> tuple:
    item = urlsplit(url)
    if item.scheme not in {"http", "https"} or not item.hostname or item.username or item.password:
        raise ValueError("Replay requires an HTTP(S) URL without embedded credentials")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("URL contains control characters")
    return (
        item.scheme,
        item.hostname.lower().rstrip("."),
        item.port or (443 if item.scheme == "https" else 80),
    )


def _headers(value: object) -> list[list[str]]:
    pairs = list(value.items()) if isinstance(value, dict) else value
    if not isinstance(pairs, (list, tuple)) or len(pairs) > 200:
        raise ValueError("Headers must be a list of name/value pairs (maximum 200)")
    result = []
    size = 0
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError("Invalid header pair")
        name, content = pair
        if not isinstance(name, str) or not isinstance(content, str) or not _TOKEN.fullmatch(name):
            raise ValueError("Invalid header name or value")
        if any(ord(char) < 32 or ord(char) == 127 for char in content):
            raise ValueError("Header values cannot contain control characters")
        try:
            size += len(name.encode("ascii")) + len(content.encode("latin-1"))
        except UnicodeError as exc:
            raise ValueError("Header values must be HTTP bytes (Latin-1)") from exc
        if size > 65536:
            raise ValueError("Headers exceed 64 KiB")
        result.append([name, content])
    return result


def prepare_request(task: dict, flow: dict, modifications: dict | None = None) -> dict:
    changes = modifications or {}
    if not isinstance(changes, dict) or set(changes) - {"url", "method", "headers", "body", "body_base64"}:
        raise ValueError("Supported changes: url, method, headers, body or body_base64")
    original = flow.get("request") or {}
    if original.get("truncated") or original.get("body_complete") is False:
        raise ValueError("Cannot replay an incomplete or truncated request body")
    original_url = original.get("url") or flow.get("url", "")
    url = changes.get("url", original_url)
    if not isinstance(url, str) or _origin(url) != _origin(original_url):
        raise ValueError("Cross-origin replay is not supported; choose a flow from the destination origin")
    scope = task.get("scope") or task
    if not allowed_url(url, scope.get("allow_hosts", []), scope.get("exclude_hosts", [])):
        raise ValueError("Replay URL is outside the current allowlist")
    method = changes.get("method", original.get("method", flow.get("method", "GET")))
    if not isinstance(method, str) or not _TOKEN.fullmatch(method) or method.upper() in {"CONNECT", "PRI"}:
        raise ValueError("Unsupported replay method")
    headers = _headers(original.get("headers", []))
    if "headers" in changes:
        patch = _headers(changes["headers"])
        if any(name.lower() in _HOP for name, _ in patch):
            raise ValueError(
                "Host, framing, connection and proxy headers are managed by the replay transport"
            )
        if isinstance(changes["headers"], dict):
            replaced = {name.lower() for name, _ in patch}
            headers = [pair for pair in headers if pair[0].lower() not in replaced] + patch
        else:
            headers = patch
    # Remove additional hop-by-hop fields named by the original Connection.
    connection_fields = {
        entry.strip().lower()
        for name, content in headers
        if name.lower() == "connection"
        for entry in content.split(",")
    }
    headers = [pair for pair in headers if pair[0].lower() not in _HOP | connection_fields]
    if "body" in changes and "body_base64" in changes:
        raise ValueError("Specify either body or body_base64")
    if "body" in changes:
        if not isinstance(changes["body"], str):
            raise ValueError("body must be text; use body_base64 for binary requests")
        body = changes["body"].encode("utf-8")
    else:
        encoded = changes.get("body_base64", original.get("body_base64", ""))
        if not isinstance(encoded, str) or len(encoded) > REQUEST_LIMIT * 2:
            raise ValueError("Invalid or oversized base64 request body")
        try:
            body = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid base64 request body") from exc
    if len(body) > REQUEST_LIMIT:
        raise ValueError("Request body exceeds 1 MiB replay limit")
    return {
        "method": method.upper(),
        "url": url,
        "headers": headers,
        "body_base64": base64.b64encode(body).decode("ascii"),
        "body_size": len(body),
        "truncated": False,
        "body_complete": True,
        "http_version": "HTTP/1.1",
    }


def run(payload: dict) -> dict:
    task = payload["task"]
    request = prepare_request(task, {"request": payload["request"]})
    url = urlsplit(request["url"])
    scheme, host, port = _origin(request["url"])
    path = urlunsplit(
        (
            "",
            "",
            quote(url.path or "/", safe="/%:@!$&'()*+,;=-._~"),
            quote(url.query, safe="/%?:@!$&'()*+,;=-._~"),
            "",
        )
    )
    body = base64.b64decode(request["body_base64"])
    # The wire Host/Content-Length are explicit in returned evidence.
    authority = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    request["headers"] += [["Host", authority], ["Content-Length", str(len(body))], ["Connection", "close"]]
    started = time.time()
    result = {
        "id": uuid.uuid4().hex,
        "session_id": payload.get("session_id", "replay"),
        "source": "agent" if payload.get("test_id") else "replay",
        "test_id": payload.get("test_id", ""),
        "created_at": datetime.fromtimestamp(started, UTC).isoformat(),
        "method": request["method"],
        "url": request["url"],
        "host": host,
        "path": path,
        "request": request,
        "response": None,
        "status_code": None,
        "content_type": "",
        "kind": "other",
        "error": None,
        "truncated": False,
        "scope_revision": (task.get("scope") or task).get("scope_revision", 0),
    }
    connection = None
    try:
        connection = (
            http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
            if scheme == "https"
            else http.client.HTTPConnection(host, port, timeout=TIMEOUT)
        )
        # Revalidate immediately before the only network send. No redirect is followed.
        prepare_request(task, {"request": request})
        connection.putrequest(request["method"], path, skip_host=True, skip_accept_encoding=True)
        for name, value in request["headers"]:
            connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        pairs = [[name, value] for name, value in response.getheaders()]
        result["status_code"] = response.status
        result["content_type"] = response.getheader("content-type", "")
        response_data = {
            "status_code": response.status,
            "headers": pairs,
            "body_base64": "",
            "body_size": 0,
            "truncated": False,
            "body_complete": False,
            "http_version": "HTTP/1.0" if response.version == 10 else "HTTP/1.1",
        }
        result["response"] = response_data
        received = bytearray()
        deadline = time.monotonic() + TIMEOUT
        while len(received) <= BODY_LIMIT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Response exceeded replay time limit")
            if connection.sock is not None:
                connection.sock.settimeout(min(remaining, 2))
            chunk = response.read1(min(65536, BODY_LIMIT + 1 - len(received)))
            if not chunk:
                response_data["body_complete"] = True
                break
            received.extend(chunk)
            response_data.update(
                body_base64=base64.b64encode(received[:BODY_LIMIT]).decode("ascii"),
                body_size=len(received),
                truncated=len(received) > BODY_LIMIT,
            )
        result["truncated"] = response_data["truncated"]
        media = result["content_type"].lower()
        result["kind"] = (
            "page"
            if "text/html" in media
            else "api"
            if "json" in media or "/api/" in path
            else "asset"
            if media.startswith(("image/", "font/", "audio/", "video/"))
            else "other"
        )
    except (TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
    finally:
        if connection is not None:
            connection.close()
    result["duration_ms"] = round((time.time() - started) * 1000)
    result["stage"] = "error" if result["error"] else "complete"
    return result


def main() -> None:
    source, destination = Path(sys.argv[1]), Path(sys.argv[2])
    if source.stat().st_size > 3 * REQUEST_LIMIT:
        raise ValueError("Replay input too large")

    def deadline(_signum, _frame):
        raise TimeoutError("Replay worker exceeded its 30 second lifetime")

    # The console may exit while this process is resolving DNS or receiving a
    # response. Enforce a process-local bound as well as the host-side deadline.
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(30)
    try:
        result = run(json.loads(source.read_text()))
    finally:
        signal.alarm(0)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=True))
    temporary.chmod(0o600)
    temporary.replace(destination)


if __name__ == "__main__":
    main()
