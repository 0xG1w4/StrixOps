"""Caido proxy — bootstrap the in-container sidecar and expose its GraphQL API.

The shared sandbox image starts Caido on port 48080 via its
entrypoint. Tools use ``127.0.0.1:48080`` inside the container. This module:

1. Logs in as guest (via container-side curl) to get an access token
2. Creates a temporary project
3. Exposes a thin GraphQL client for the proxy tool family

The proxy tool family lives in ``tools/proxy.py``; this module is the
connection layer only. No ``caido_sdk_client`` dependency — we call the
GraphQL API directly with ``requests``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

import requests as _requests

logger = logging.getLogger("strixops.caido")

DIAGNOSTIC_TIMEOUT = 5.0
# This runs inside the sandbox. It emits only sanitized diagnostics, never the
# local guest credential or captured raw traffic. All HTTP is to loopback Caido.
_DIAGNOSTIC_SCRIPT = r"""
import base64
import json
import os
import re
import stat
import sys
import urllib.request
from html.parser import HTMLParser

LOG_PATH = "/tmp/caido_startup.log"
LOG_LIMIT = 16384
SAMPLE_LIMIT = 3
ERROR_PHRASES = (
    "connection refused", "connection reset", "connection closed", "connection aborted",
    "network is unreachable", "no route to host", "connection timed out", "operation timed out",
    "timed out", "timeout", "dns error", "dns lookup failed", "failed to lookup address information",
    "certificate verify failed", "invalid peer certificate", "unknown issuer", "certificate expired",
    "tls handshake", "handshake failure", "unexpected eof", "error trying to connect",
    "error sending request", "client error (connect)", "incomplete message", "invalid http header",
    "request cancelled",
)

def sanitize_log(text):
    for name, value in os.environ.items():
        if value and len(value) >= 4 and re.search(r"key|token|secret|pass", name, re.I):
            text = text.replace(value, "[redacted]")
    lines = []
    for line in text.splitlines():
        if re.search(
            r"authorization|cookie|api[_ -]?key|token|secret|password|passphrase|"
            r"private.key|credential|bearer",
            line, re.I,
        ):
            lines.append("[redacted credential-bearing line]")
            continue
        line = re.sub(r"https?://\S+", "[URL omitted]", line, flags=re.I)
        line = re.sub(r"[A-Za-z0-9_+/.=-]{32,}", "[redacted value]", line)
        lines.append(line)
    return "\n".join(lines)

def startup_log():
    result = {"source": LOG_PATH}
    try:
        fd = os.open(LOG_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not a regular log file")
            stream.seek(max(0, info.st_size - LOG_LIMIT))
            raw = stream.read(LOG_LIMIT)
        result.update(status="ok", truncated=info.st_size > LOG_LIMIT,
                      tail=sanitize_log(raw.decode("utf-8", errors="replace")))
    except Exception as exc:
        result.update(status="unavailable", error_type=type(exc).__name__)
    return result

class ErrorPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.in_title = False
        self.title = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if not self.hidden:
            self.text.append(data)

def summarize_response(node):
    response = node.get("response") or {}
    result = {"request_id": str(node.get("id", ""))[:80], "status_code": response.get("statusCode")}
    encoded = response.get("raw")
    if not isinstance(encoded, str) or len(encoded) > 262144:
        return dict(result, detail_status="raw_unavailable_or_too_large")
    try:
        raw = base64.b64decode(encoded, validate=True)
        _, separator, body = raw.partition(b"\r\n\r\n")
        if not separator:
            _, separator, body = raw.partition(b"\n\n")
        page = ErrorPage()
        page.feed(body.decode("utf-8", errors="replace"))
        if "".join(page.title).strip().lower() != "caido":
            return dict(result, detail_status="non_caido_response_omitted")
        text = " ".join(page.text).lower()
        markers = [phrase for phrase in ERROR_PHRASES if phrase in text]
        # Persist fixed phrases only; error pages may embed URLs or credentials.
        return dict(result, page_title="Caido", error_markers=markers,
                    detail_status="recognized_error" if markers else "no_recognized_error_detail")
    except Exception as exc:
        return dict(result, detail_status="unparseable", error_type=type(exc).__name__)

def captured_errors():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, file, code, message, headers, url):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def query(document, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            "http://127.0.0.1:48080/graphql",
            data=json.dumps({"query": document}).encode(), headers=headers, method="POST",
        )
        with opener.open(request, timeout=1.0) as response:
            raw = response.read(524289)
        if len(raw) > 524288:
            raise ValueError("diagnostic response too large")
        payload = json.loads(raw)
        if payload.get("errors") or not isinstance(payload.get("data"), dict):
            raise ValueError("diagnostic GraphQL unavailable")
        return payload["data"]

    try:
        login = query("mutation { loginAsGuest { token { accessToken } } }")
        token = (((login.get("loginAsGuest") or {}).get("token") or {}).get("accessToken"))
        if not isinstance(token, str) or not token:
            raise ValueError("guest login unavailable")
        data = query('query { requests(first: 3, filter: {code: "resp.code.eq:502"}, '
                     'order: {by: CREATED_AT, ordering: DESC}) { '
                     'edges { node { id response { statusCode raw } } } } }', token)
        edges = data["requests"]["edges"]
        return {"status": "ok", "sample_limit": SAMPLE_LIMIT,
                "coverage_note": "Caido-generated 502s may have no stored response and be omitted here; "
                                 "empty samples do not rule out proxy errors. Consult startup_log.",
                "samples": [summarize_response(edge["node"]) for edge in edges[:SAMPLE_LIMIT]]}
    except Exception as exc:
        return {"status": "unavailable", "error_type": type(exc).__name__}

def main():
    result = startup_log() if sys.argv[1] == "startup_log" else captured_errors()
    print(json.dumps(result), flush=True)

if __name__ == "__main__":
    main()
"""


async def collect_diagnostics(session: Any, diagnostics_dir: Path | str) -> None:
    """Persist a small local snapshot; diagnostic failures never block cleanup."""
    report: dict[str, Any] = {
        "source": "sandbox-local Caido diagnostics",
        "startup_log": {"status": "not_collected"},
        "captured_http_502": {"status": "not_collected"},
    }

    async def collect_part(key: str) -> None:
        try:
            result = await asyncio.wait_for(
                session.exec("python3", "-c", _DIAGNOSTIC_SCRIPT, key, timeout=4),
                timeout=DIAGNOSTIC_TIMEOUT,
            )
            if not result.ok():
                report[key] = {"status": "unavailable", "error_type": "exec_failed"}
                return
            output = (
                result.stdout.decode("utf-8", errors="replace")
                if isinstance(result.stdout, bytes)
                else str(result.stdout)
            )
            if len(output) > 65536:
                raise ValueError("diagnostic output too large")
            record = json.loads(output)
            if not isinstance(record, dict):
                raise ValueError("invalid diagnostic output")
            report[key] = record
        except asyncio.CancelledError:
            report[key] = {"status": "unavailable", "error_type": "cancelled"}
        except Exception as exc:
            report[key] = {"status": "unavailable", "error_type": type(exc).__name__}

    # Separate bounded calls retain the log even when the local GraphQL API is
    # unavailable. Their combined wall time is bounded by the same deadline.
    try:
        await asyncio.gather(collect_part("startup_log"), collect_part("captured_http_502"))
    except asyncio.CancelledError:
        report["collection_error"] = {"type": "cancelled"}
    try:
        directory = Path(diagnostics_dir)
        directory.mkdir(parents=True, exist_ok=True)
        output_path = directory / "caido.json"
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        output_path.chmod(0o600)
    except Exception as exc:
        logger.warning("Could not persist Caido diagnostics (%s)", type(exc).__name__)


CAIDO_CONTAINER_PORT = 48080
CAIDO_CONTAINER_URL = f"http://127.0.0.1:{CAIDO_CONTAINER_PORT}"

_LOGIN_MUTATION = "mutation { loginAsGuest { token { accessToken } } }"
_CREATE_PROJECT_MUTATION = """
mutation($input: CreateProjectInput!) {
  createProject(input: $input) { project { id name } error { __typename } }
}
"""
_SELECT_PROJECT_MUTATION = """
mutation($id: ID!) {
  selectProject(id: $id) { currentProject { project { id } } error { __typename } }
}
"""


async def login_as_guest(session: Any, *, attempts: int = 10) -> str:
    """Exec curl inside the container to fetch a Caido guest token.

    Doubles as the readiness probe — Caido may not be up the instant the
    container starts.
    """
    last_err: str | None = None
    for i in range(1, attempts + 1):
        try:
            result = await session.exec(
                "curl",
                "-fsS",
                "--max-time",
                "10",
                "--noproxy",
                "*",
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps({"query": _LOGIN_MUTATION}),
                f"{CAIDO_CONTAINER_URL}/graphql",
                timeout=15,
            )
            if result.ok():
                payload = json.loads(result.stdout)
                token = (((payload.get("data") or {}).get("loginAsGuest") or {}).get("token") or {}).get(
                    "accessToken"
                )
                if token:
                    return str(token)
                last_err = "loginAsGuest returned no access token"
            else:
                stderr = (
                    result.stderr.decode("utf-8", errors="replace")[:200]
                    if isinstance(result.stderr, bytes)
                    else str(result.stderr)[:200]
                )
                last_err = f"curl exit {result.exit_code}: {stderr}"
        except (TimeoutError, ValueError, AttributeError) as exc:
            last_err = f"loginAsGuest response unavailable ({type(exc).__name__})"
        logger.debug("loginAsGuest attempt %d/%d failed: %s", i, attempts, last_err)
        if i < attempts:
            await asyncio.sleep(min(2.0 * i, 8.0))

    raise RuntimeError(f"Caido loginAsGuest failed after {attempts} attempts: {last_err}")


class CaidoClient:
    """Thin GraphQL client for the in-container Caido sidecar."""

    def __init__(self, host_url: str, token: str) -> None:
        self._url = f"{host_url}/graphql"
        self._session = _requests.Session()
        # This is the Docker-published control endpoint, not target traffic.
        self._session.trust_env = False
        self._lock = threading.RLock()
        self._closed = False
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )
        self._project_id: str | None = None

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GraphQL query/mutation; returns the ``data`` dict."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Caido client is closed")
            resp = self._session.post(
                self._url,
                json={"query": query, "variables": variables or {}},
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"Caido GraphQL error: {body['errors']}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Caido GraphQL returned no data")
        return data

    def create_and_select_project(self) -> str:
        """Create a temporary project and select it (fresh capture state)."""
        data = self.graphql(_CREATE_PROJECT_MUTATION, {"input": {"name": "strixops", "temporary": True}})
        created = data.get("createProject") or {}
        project_id = (created.get("project") or {}).get("id")
        if created.get("error") or not project_id:
            raise RuntimeError(f"Caido createProject returned no id: {data}")
        data = self.graphql(_SELECT_PROJECT_MUTATION, {"id": project_id})
        selected = data.get("selectProject") or {}
        selected_id = ((selected.get("currentProject") or {}).get("project") or {}).get("id")
        if selected.get("error") or selected_id != project_id:
            raise RuntimeError(f"Caido selectProject failed: {data}")
        self._project_id = project_id
        logger.info("Caido project selected: %s", project_id)
        return project_id

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._session.close()


async def bootstrap_caido(session: Any, *, host_url: str) -> CaidoClient:
    """Connect to the in-container Caido sidecar and select a fresh project."""

    logger.info("Bootstrapping Caido (host=%s)", host_url)
    token = await login_as_guest(session)
    client = CaidoClient(host_url, token)
    setup = asyncio.create_task(asyncio.to_thread(client.create_and_select_project))
    try:
        await asyncio.shield(setup)
    except BaseException:
        # A requests call cannot be cancelled midway. Finish it before closing
        # the shared Session, including when a scan ends during bootstrap.
        with contextlib.suppress(Exception):
            await setup
        await asyncio.to_thread(client.close)
        raise
    return client


class CaidoBootstrapHandle:
    """Share background initialization; cancelling a caller leaves it running."""

    def __init__(self, task: asyncio.Task[CaidoClient]) -> None:
        self._task = task
        task.add_done_callback(self._finished)

    @staticmethod
    def _finished(task: asyncio.Task[CaidoClient]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("Caido bootstrap failed (proxy tools disabled): %s", error)

    async def get(self) -> CaidoClient:
        return await asyncio.shield(self._task)

    async def aclose(self) -> None:
        if not self._task.done():
            self._task.cancel()
        try:
            client = await self._task
        except (asyncio.CancelledError, Exception):
            return
        await asyncio.to_thread(client.close)


# ---- Proxy env vars for tools that support HTTP proxy configuration ----


def proxy_environment() -> dict[str, str]:
    """Route proxy-aware HTTP clients through Caido (not raw TCP/UDP scans)."""
    return {
        "http_proxy": CAIDO_CONTAINER_URL,
        "https_proxy": CAIDO_CONTAINER_URL,
        "HTTP_PROXY": CAIDO_CONTAINER_URL,
        "HTTPS_PROXY": CAIDO_CONTAINER_URL,
        "ALL_PROXY": CAIDO_CONTAINER_URL,
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    }
