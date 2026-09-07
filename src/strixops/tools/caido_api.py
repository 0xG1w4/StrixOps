"""Direct GraphQL protocol and HTTP serialization for the bundled Caido.

The wire shapes match the Caido SDK 0.2.0 protocol used by the reference
scanner. No SDK is imported: the runtime owns the authenticated HTTP client.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REQUEST_METADATA = """
id host port method path query isTls createdAt
response { id statusCode roundtripTime length createdAt }
"""
REQUEST_LIST = f"""
query ProxyRequests($first: Int!, $after: String, $filter: HTTPQLInput,
                    $order: RequestResponseOrderInput, $scopeId: ID) {{
  requests(first: $first, after: $after, filter: $filter, order: $order, scopeId: $scopeId) {{
    edges {{ cursor node {{ {REQUEST_METADATA} }} }}
    pageInfo {{ hasNextPage hasPreviousPage startCursor endCursor }}
  }}
}}
"""
REQUEST_GET = """
query ProxyRequest($id: ID!) {
  request(id: $id) {
    id host port method path query isTls createdAt raw
    response { id statusCode roundtripTime length createdAt raw }
  }
}
"""
SITEMAP_FIELDS = """
id kind label hasDescendants
metadata { ... on SitemapEntryMetadataDomain { isTls port } }
request { method path response { statusCode length roundtripTime } }
"""
SITEMAP_ROOTS = f"""
query ProxySitemapRoots($scopeId: ID) {{
  sitemapRootEntries(scopeId: $scopeId) {{
    edges {{ node {{ {SITEMAP_FIELDS} }} }} count {{ value }}
  }}
}}
"""
SITEMAP_CHILDREN = f"""
query ProxySitemapChildren($parentId: ID!, $depth: SitemapDescendantsDepth!) {{
  sitemapDescendantEntries(parentId: $parentId, depth: $depth) {{
    edges {{ node {{ {SITEMAP_FIELDS} }} }} count {{ value }}
  }}
}}
"""
SITEMAP_GET = f"""
query ProxySitemapEntry($id: ID!) {{
  sitemapEntry(id: $id) {{
    {SITEMAP_FIELDS}
    requests(first: 30, order: {{by: CREATED_AT, ordering: DESC}}) {{
      edges {{ node {{ method path response {{ statusCode length }} }} }}
      count {{ value }}
    }}
  }}
}}
"""
SCOPE_FIELDS = "id name allowlist denylist indexed"
SCOPES = f"query ProxyScopes {{ scopes {{ {SCOPE_FIELDS} }} }}"
SCOPE_GET = f"query ProxyScope($id: ID!) {{ scope(id: $id) {{ {SCOPE_FIELDS} }} }}"
SCOPE_ERROR = """
error { __typename ... on InvalidGlobTermsUserError { code }
        ... on OtherUserError { code } }
"""
SCOPE_CREATE = f"""
mutation ProxyCreateScope($input: CreateScopeInput!) {{
  createScope(input: $input) {{ {SCOPE_ERROR} scope {{ {SCOPE_FIELDS} }} }}
}}
"""
SCOPE_UPDATE = f"""
mutation ProxyUpdateScope($id: ID!, $input: UpdateScopeInput!) {{
  updateScope(id: $id, input: $input) {{ {SCOPE_ERROR} scope {{ {SCOPE_FIELDS} }} }}
}}
"""
SCOPE_DELETE = "mutation ProxyDeleteScope($id: ID!) { deleteScope(id: $id) { deletedId } }"
REPLAY_CREATE = """
mutation ProxyCreateReplay($input: CreateReplaySessionInput!) {
  createReplaySession(input: $input) { session { id } }
}
"""
REPLAY_START = """
mutation ProxyStartReplay($sessionId: ID!, $input: StartReplayTaskInput!) {
  startReplayTask(sessionId: $sessionId, input: $input) {
    error { __typename ... on OtherUserError { code }
            ... on CloudUserError { code } ... on PermissionDeniedUserError { code }
            ... on TaskInProgressUserError { code } }
    task { id replayEntry { id } }
  }
}
"""
REPLAY_POLL = """
query ProxyReplayResult($id: ID!) {
  tasks { id }
  replayEntry(id: $id) {
    id error request { id response { id statusCode roundtripTime length raw } }
  }
}
"""
REPLAY_TIMEOUT = 30.0
REPLAY_POLL_INTERVAL = 0.2
SORT_FIELDS = {
    "timestamp": "CREATED_AT",
    "host": "HOST",
    "method": "METHOD",
    "path": "PATH",
    "status_code": "RESP_STATUS_CODE",
    "response_time": "RESP_ROUNDTRIP_TIME",
    "response_size": "RESP_LENGTH",
    "source": "SOURCE",
}


async def graphql(client: Any, document: str, variables: dict | None = None) -> dict:
    """Keep synchronous requests and the client's lock off the agent event loop."""
    return await asyncio.to_thread(client.graphql, document, variables)


def payload(data: dict, operation: str, field: str) -> Any:
    result = data.get(operation) or {}
    if result.get("error"):
        raise RuntimeError(f"{operation}: {json.dumps(result['error'], ensure_ascii=False)}")
    if result.get(field) is None:
        raise RuntimeError(f"{operation} returned no {field}")
    return result[field]


def raw_bytes(value: str | None) -> bytes | None:
    return base64.b64decode(value, validate=True) if value is not None else None


def timestamp(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    return value


def response_metadata(response: dict | None) -> dict | None:
    if response is None:
        return None
    result = {
        "id": response.get("id"),
        "status_code": response.get("statusCode"),
        "length": response.get("length"),
        "created_at": timestamp(response.get("createdAt")),
    }
    if response.get("roundtripTime"):
        result["roundtrip_ms"] = response["roundtripTime"]
    return result


def request_entry(edge: dict) -> dict:
    node = edge["node"]
    return {
        "cursor": edge.get("cursor"),
        "request": {
            **{key: node.get(key) for key in ("id", "host", "port", "method", "path", "query")},
            "is_tls": node.get("isTls"),
            "created_at": timestamp(node.get("createdAt")),
        },
        "response": response_metadata(node.get("response")),
    }


def sitemap_node(node: dict) -> dict:
    result = {
        "id": node["id"],
        "kind": node.get("kind"),
        "label": node.get("label"),
        "has_descendants": node.get("hasDescendants", False),
    }
    if node.get("metadata"):
        meta = node["metadata"]
        result["metadata"] = {"is_tls": meta.get("isTls"), "port": meta.get("port")}
    if node.get("request"):
        result["request"] = sitemap_request(node["request"])
    return result


def sitemap_request(node: dict) -> dict:
    result = {key: node[key] for key in ("method", "path") if key in node}
    response = node.get("response")
    if response:
        result["status_code"] = response.get("statusCode")
        result["response"] = {"status_code": response.get("statusCode"), "length": response.get("length")}
        if response.get("roundtripTime"):
            result["response"]["roundtrip_ms"] = response["roundtripTime"]
    return result


def split_http(raw: bytes) -> tuple[str, list[tuple[str, str]], bytes]:
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        head, separator, body = raw.partition(b"\n\n")
    lines = head.decode("iso-8859-1").splitlines()
    if not lines:
        raise ValueError("Empty HTTP message")
    headers = [tuple(line.split(":", 1)) for line in lines[1:] if ":" in line]
    return lines[0], [(name.strip(), value.strip()) for name, value in headers], body


def _replace_header(headers: list[tuple[str, str]], name: str, value: Any) -> None:
    headers[:] = [(key, val) for key, val in headers if key.lower() != name.lower()]
    values = value if isinstance(value, list) else [value]
    headers.extend((name, str(item)) for item in values)


def _dechunk(body: bytes) -> bytes:
    """Remove wire chunk framing before changing to Content-Length framing."""
    chunks = []
    remaining = body
    while True:
        size_line, separator, remaining = remaining.partition(b"\r\n")
        if not separator:
            raise ValueError("Captured chunked body has no chunk size line")
        size = int(size_line.partition(b";")[0], 16)
        if size == 0:
            return b"".join(chunks)
        if size < 0 or len(remaining) < size + 2 or remaining[size : size + 2] != b"\r\n":
            raise ValueError("Captured chunked body is incomplete")
        chunks.append(remaining[:size])
        remaining = remaining[size + 2 :]


def prepare_replay(original: dict, modifications: dict) -> tuple[dict, bytes]:
    """Patch a captured message, retaining unmodified bytes and repeated headers."""
    raw = raw_bytes(original.get("raw"))
    if raw is None:
        raise ValueError("Captured request has no raw content")
    request_line, headers, body = split_http(raw)
    method, target, *_ = request_line.split(" ", 2)
    scheme = "https" if original.get("isTls") else "http"
    host = str(original["host"])
    authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
    port = original.get("port") or (443 if scheme == "https" else 80)
    if port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    captured_target = urlsplit(target)
    path = captured_target.path or original.get("path") or "/"
    query = captured_target.query or original.get("query") or ""
    url = str(modifications.get("url") or urlunsplit((scheme, authority, path, query, "")))
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Replay URL must be an absolute http or https URL")
    if "path" in modifications:
        new_path = str(modifications["path"])
        replacement = urlsplit(new_path)
        parts = parts._replace(
            path=replacement.path or "/", query=replacement.query if "?" in new_path else parts.query
        )
    if "params" in modifications:
        params = modifications["params"]
        if not isinstance(params, dict):
            raise ValueError("modifications.params must be an object")
        pairs = [
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in params
        ]
        pairs.extend((str(key), value) for key, value in params.items())
        parts = parts._replace(query=urlencode(pairs, doseq=True))

    supplied_headers = modifications.get("headers", {})
    if not isinstance(supplied_headers, dict):
        raise ValueError("modifications.headers must be an object")
    supplied_headers = dict(supplied_headers)
    supplied_headers.update(
        {key[7:]: value for key, value in modifications.items() if key.startswith("header_")}
    )
    explicit_host = any(str(key).lower() == "host" for key in supplied_headers)
    host_headers = [value for key, value in headers if key.lower() == "host"]
    if not explicit_host:
        _replace_header(
            headers, "Host", parts.netloc if "url" in modifications or not host_headers else host_headers[-1]
        )
    if "body" in modifications:
        body = str(modifications["body"]).encode("utf-8")
    elif any(key.lower() == "transfer-encoding" and "chunked" in value.lower() for key, value in headers):
        body = _dechunk(body)
    for name, value in supplied_headers.items():
        _replace_header(headers, str(name), value)
    if "cookies" in modifications:
        updates = modifications["cookies"]
        if not isinstance(updates, dict):
            raise ValueError("modifications.cookies must be an object")
        cookies = {}
        for name, value in headers:
            if name.lower() == "cookie":
                for pair in value.split(";"):
                    key, separator, val = pair.strip().partition("=")
                    if separator:
                        cookies[key] = val
        cookies.update(updates)
        _replace_header(headers, "Cookie", "; ".join(f"{key}={value}" for key, value in cookies.items()))
    had_length = any(name.lower() == "content-length" for name, _ in headers)
    headers = [
        (name, value)
        for name, value in headers
        if name.lower() not in ("content-length", "transfer-encoding", "trailer")
    ]
    if body or had_length:
        headers.append(("Content-Length", str(len(body))))
    method = str(modifications.get("method", method)).upper()
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    head = f"{method} {target} HTTP/1.1\r\n" + "".join(f"{key}: {val}\r\n" for key, val in headers)
    connection = {
        "host": parts.hostname,
        "port": parts.port or (443 if parts.scheme == "https" else 80),
        "isTLS": parts.scheme == "https",
        "SNI": None,
    }
    return connection, head.encode("iso-8859-1") + b"\r\n" + body


def replay_response(response: dict | None) -> dict | None:
    if response is None:
        return None
    result = response_metadata(response) or {}
    raw = raw_bytes(response.get("raw"))
    if raw is not None:
        _, header_pairs, body = split_http(raw)
        result["headers"] = {name: value for name, value in header_pairs}
        result["length"] = len(body)
        text = body.decode("utf-8", errors="replace")
        result["body"] = text[:8192]
        result["body_truncated"] = len(text) > 8192
    return result


async def send_replay(client: Any, connection: dict, raw: bytes) -> dict:
    """Create an empty session, dispatch once, then poll the saved result.

    HTTP polling avoids the SDK's websocket dependency. A completed task with
    no response is reported as an error; it is never inferred to have succeeded.
    """
    started = time.monotonic()
    data = await graphql(client, REPLAY_CREATE, {"input": {}})
    session = payload(data, "createReplaySession", "session")
    data = await graphql(
        client,
        REPLAY_START,
        {
            "sessionId": session["id"],
            "input": {
                "connection": connection,
                "raw": base64.b64encode(raw).decode("ascii"),
                "settings": {"connectionClose": False, "updateContentLength": True, "placeholders": []},
            },
        },
    )
    task = payload(data, "startReplayTask", "task")
    entry_id = (task.get("replayEntry") or {}).get("id")
    if not entry_id:
        raise RuntimeError("startReplayTask returned no replay entry")

    async def wait_for_result() -> dict:
        while True:
            data = await graphql(client, REPLAY_POLL, {"id": entry_id})
            entry = data.get("replayEntry") or {}
            request = entry.get("request") or {}
            response = request.get("response")
            error = entry.get("error")
            active = any(str(item["id"]) == str(task["id"]) for item in data.get("tasks", []))
            if error or not active:
                error = error or (
                    None if response else "Replay ended without a response (failed or cancelled)"
                )
                return {
                    "success": error is None,
                    "status": "ERROR" if error else "DONE",
                    "error": error,
                    "response": replay_response(response),
                    "request_id": request.get("id"),
                }
            await asyncio.sleep(REPLAY_POLL_INTERVAL)

    try:
        result = await asyncio.wait_for(wait_for_result(), timeout=REPLAY_TIMEOUT)
    except TimeoutError:
        result = {
            "success": False,
            "status": "ERROR",
            "response": None,
            "error": f"Replay completion was not confirmed within {REPLAY_TIMEOUT:g}s; "
            "the dispatched task may still be running.",
        }
    result.update(session_id=session["id"], elapsed_ms=int((time.monotonic() - started) * 1000))
    return result
