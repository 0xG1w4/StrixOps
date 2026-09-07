"""Caido tools for captured requests, replay, sitemap traversal and scope CRUD.

Argument names and result shapes follow the platform's proxy event contract.
The HTTP transport is owned by the sandbox; tools await lazy bootstrap and
run blocking GraphQL calls in a worker thread.
"""

from __future__ import annotations

import builtins
import json
import re
from typing import Any, Literal

from agents import RunContextWrapper, function_tool

from strixops.tools import caido_api as api

SortBy = Literal[
    "timestamp", "host", "method", "path", "status_code", "response_time", "response_size", "source"
]
SortOrder = Literal["asc", "desc"]
RequestPart = Literal["request", "response", "both"]
SitemapDepth = Literal["DIRECT", "ALL"]
ScopeAction = Literal["list", "get", "create", "update", "delete"]


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _error(exc: Exception) -> str:
    return _json({"success": False, "error": str(exc)})


async def _client(ctx: RunContextWrapper) -> Any:
    services = getattr(ctx.context, "services", None)
    sandbox = getattr(services, "sandbox", None)
    client = getattr(sandbox, "caido", None)
    if client is not None and not hasattr(client, "graphql"):
        client = await client.get()
    if client is None:
        raise RuntimeError("Proxy is not active for this scan (Caido is unavailable or disabled).")
    return client


def _optional(value: str | None) -> str | None:
    return None if value is None or value.strip().lower() in ("", "null", "none") else value


def _page_info(connection: dict) -> dict:
    page = connection.get("pageInfo") or {}
    return {
        "has_next_page": page.get("hasNextPage", False),
        "has_previous_page": page.get("hasPreviousPage", False),
        "start_cursor": page.get("startCursor"),
        "end_cursor": page.get("endCursor"),
    }


@function_tool(timeout=120, strict_mode=False)
async def list_requests(
    ctx: RunContextWrapper,
    httpql_filter: str | None = None,
    first: int = 50,
    after: str | None = None,
    sort_by: SortBy = "timestamp",
    sort_order: SortOrder = "desc",
    scope_id: str | None = None,
    max: int | None = None,
    offset: int = 0,
) -> str:
    """List captured traffic with HTTPQL, sorting, scopes and cursor pagination.

    Args:
        httpql_filter: Caido HTTPQL, e.g. `resp.code.eq:200 AND req.path.cont:"/api"`.
            Strings must be quoted; numbers must not. Other examples:
            `req.method.eq:"POST"`, `req.host.regex:".*example.com"`,
            `req.raw.cont:"secret"`. Use AND/OR and negated operators such
            as `ncont` or `ne`; HTTPQL has no NOT operator.
        first: Page size (1-100, default 50).
        after: Previous page_info.end_cursor, or omit for the first page.
        sort_by: timestamp, host, method, path, status_code, response_time,
            response_size, or source.
        sort_order: asc or desc.
        scope_id: Optional scope created by scope_rules.
        max: Compatibility alias for first; overrides first when supplied.
        offset: Compatibility offset; skips requests by walking Caido cursors.
    """
    try:
        client = await _client(ctx)
        size = builtins.max(1, min(max if max is not None else first, 100))
        skip = builtins.max(0, offset)
        variables = {
            "first": size,
            "after": _optional(after),
            "scopeId": _optional(scope_id),
            "filter": {"code": httpql_filter} if _optional(httpql_filter) else None,
            "order": {"by": api.SORT_FIELDS[sort_by], "ordering": sort_order.upper()},
        }
        # Caido has no numeric offset: consume real cursor pages rather than
        # inventing cursor encodings or sending an unsupported argument.
        while skip:
            data = await api.graphql(client, api.REQUEST_LIST, {**variables, "first": min(skip, 100)})
            connection = data["requests"]
            entries = connection.get("edges") or []
            page = _page_info(connection)
            skip -= len(entries)
            if not page["has_next_page"] or not entries:
                return _json(
                    {
                        "success": True,
                        "entries": [],
                        "page_info": {
                            **page,
                            "start_cursor": None,
                            "end_cursor": None,
                        },
                    }
                )
            cursor = page["end_cursor"]
            if not cursor or cursor == variables["after"]:
                raise RuntimeError("Caido pagination did not advance")
            variables["after"] = cursor
        data = await api.graphql(client, api.REQUEST_LIST, variables)
        connection = data["requests"]
        return _json(
            {
                "success": True,
                "entries": [api.request_entry(edge) for edge in connection.get("edges") or []],
                "page_info": _page_info(connection),
            }
        )
    except Exception as exc:
        return _error(exc)


def _content(raw: str | None, part: str, search_pattern: str | None, page: int, page_size: int) -> dict:
    decoded = api.raw_bytes(raw)
    if decoded is None:
        return {"success": False, "error": f"No raw {part} is available"}
    text = decoded.decode("utf-8", errors="replace")
    if search_pattern:
        regex = re.compile(search_pattern)
        hits = []
        for match in regex.finditer(text):
            start, end = match.span()
            hits.append(
                {
                    "match": match.group(),
                    "position": start,
                    "before": text[builtins.max(0, start - 40) : start],
                    "after": text[end : end + 40],
                }
            )
            if len(hits) == 20:
                break
        return {"success": True, "hits": hits, "total_hits": len(hits)}
    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be positive")
    lines = text.splitlines()
    start = (page - 1) * page_size
    return {
        "success": True,
        "content": "\n".join(lines[start : start + page_size]),
        "page": page,
        "page_size": page_size,
        "total_lines": len(lines),
        "has_more": start + page_size < len(lines),
    }


@function_tool(timeout=60, strict_mode=False)
async def view_request(
    ctx: RunContextWrapper,
    request_id: str,
    part: RequestPart = "request",
    search_pattern: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> str:
    """Inspect raw captured HTTP messages, or search them with a regex.

    Args:
        request_id: Request ID from list_requests.
        part: request, response, or both. Both returns separate content pages.
        search_pattern: Optional regex, returning at most 20 hits with position
            and surrounding context instead of a content page.
        page: Content page, starting at 1; ignored in regex mode.
        page_size: Lines per content page (default 50).
    """
    try:
        client = await _client(ctx)
        data = await api.graphql(client, api.REQUEST_GET, {"id": request_id})
        request = data.get("request")
        if not request:
            raise ValueError(f"Request {request_id} not found")
        pattern = _optional(search_pattern)
        response = request.get("response") or {}
        if part == "both":
            return _json(
                {
                    "success": True,
                    "id": request_id,
                    "request": _content(request.get("raw"), "request", pattern, page, page_size),
                    "response": _content(response.get("raw"), "response", pattern, page, page_size),
                }
            )
        raw = request.get("raw") if part == "request" else response.get("raw")
        return _json(_content(raw, part, pattern, page, page_size))
    except Exception as exc:
        return _error(exc)


@function_tool(timeout=120, strict_mode=False)
async def repeat_request(
    ctx: RunContextWrapper,
    request_id: str,
    modifications: dict[str, Any] | None = None,
) -> str:
    """Replay a captured request with optional URL, parameter, header or body changes.

    Args:
        request_id: Request ID from list_requests.
        modifications: Optional patch object. `url` replaces the destination URL;
            `params`, `headers`, and `cookies` are dictionaries to merge;
            `body` replaces the body. `method` changes the HTTP method.
            Compatibility aliases: `path` changes the path (an explicit query
            replaces the old query), and `header_<name>` sets a header.
            Unmodified headers/cookies/body are retained. Content-Length is
            recomputed after modifying the body.
    """
    try:
        client = await _client(ctx)
        data = await api.graphql(client, api.REQUEST_GET, {"id": request_id})
        original = data.get("request")
        if not original:
            raise ValueError(f"Request {request_id} not found")
        connection, raw = api.prepare_replay(original, modifications or {})
        return _json(await api.send_replay(client, connection, raw))
    except Exception as exc:
        return _error(exc)


@function_tool(timeout=60, strict_mode=False)
async def list_sitemap(
    ctx: RunContextWrapper,
    scope_id: str | None = None,
    parent_id: str | None = None,
    depth: SitemapDepth = "DIRECT",
    page: int = 1,
) -> str:
    """Browse root domains or expand a node in Caido's hierarchical sitemap.

    Args:
        scope_id: Filter root domains to this scope; ignored with parent_id.
        parent_id: Node to expand; omit to start at root domains.
        depth: DIRECT lists children; ALL lists the entire subtree.
        page: Page number starting at 1, with 30 entries per page.
    """
    try:
        if page < 1:
            raise ValueError("page must be positive")
        client = await _client(ctx)
        parent = _optional(parent_id)
        if parent:
            data = await api.graphql(client, api.SITEMAP_CHILDREN, {"parentId": parent, "depth": depth})
            connection = data["sitemapDescendantEntries"]
        else:
            data = await api.graphql(client, api.SITEMAP_ROOTS, {"scopeId": _optional(scope_id)})
            connection = data["sitemapRootEntries"]
        edges = connection.get("edges") or []
        total = (connection.get("count") or {}).get("value", len(edges))
        total_pages = (total + 29) // 30
        selected = edges[(page - 1) * 30 : page * 30]
        return _json(
            {
                "success": True,
                "entries": [api.sitemap_node(edge["node"]) for edge in selected],
                "page": page,
                "page_size": 30,
                "total_count": total,
                "total_pages": total_pages,
                "has_more": page < total_pages,
            }
        )
    except Exception as exc:
        return _error(exc)


@function_tool(timeout=60, strict_mode=False)
async def view_sitemap_entry(ctx: RunContextWrapper, entry_id: str) -> str:
    """Show a sitemap node's metadata and its most recent 30 related requests.

    Args:
        entry_id: Node ID from list_sitemap.
    """
    try:
        client = await _client(ctx)
        data = await api.graphql(client, api.SITEMAP_GET, {"id": entry_id})
        node = data.get("sitemapEntry")
        if not node:
            raise ValueError(f"Sitemap entry {entry_id} not found")
        entry = api.sitemap_node(node)
        related = node.get("requests") or {}
        entry["related_requests"] = {
            "requests": [api.sitemap_request(edge["node"]) for edge in related.get("edges") or []],
            "total_count": (related.get("count") or {}).get("value", 0),
        }
        return _json({"success": True, "entry": entry})
    except Exception as exc:
        return _error(exc)


@function_tool(timeout=60, strict_mode=False)
async def scope_rules(
    ctx: RunContextWrapper,
    action: ScopeAction,
    allowlist: list[str] | None = None,
    denylist: list[str] | None = None,
    scope_id: str | None = None,
    scope_name: str | None = None,
) -> str:
    """List, inspect, create, update or delete Caido query scopes.

    Scopes filter traffic shown by list_requests and list_sitemap; they do
    not block network connections. Deny patterns take precedence over allow
    patterns; an empty allowlist includes all domains. Patterns support globs.

    Args:
        action: list, get, create, update, or delete.
        allowlist: Domain/path glob patterns to include on create/update.
        denylist: Glob patterns to exclude on create/update.
        scope_id: Required for get, update and delete.
        scope_name: Name for create/update. Create defaults to strixops-scope
            for compatibility; update requires an explicit name.
    """
    try:
        client = await _client(ctx)
        identity = _optional(scope_id)
        if action == "list":
            data = await api.graphql(client, api.SCOPES)
            return _json({"success": True, "scopes": data["scopes"]})
        if action in ("get", "update", "delete") and not identity:
            raise ValueError(f"scope_id is required for action='{action}'")
        if action == "get":
            data = await api.graphql(client, api.SCOPE_GET, {"id": identity})
            if not data.get("scope"):
                raise ValueError(f"Scope {identity} not found")
            return _json({"success": True, "scope": data["scope"]})
        if action == "delete":
            data = await api.graphql(client, api.SCOPE_DELETE, {"id": identity})
            deleted = api.payload(data, "deleteScope", "deletedId")
            return _json({"success": True, "deleted": deleted, "message": f"Scope {deleted} deleted"})
        name = _optional(scope_name)
        if action == "update" and not name:
            raise ValueError("scope_name is required for action='update'")
        variables = {
            "input": {
                "name": name or "strixops-scope",
                "allowlist": allowlist or [],
                "denylist": denylist or [],
            }
        }
        if action == "create":
            document, operation = api.SCOPE_CREATE, "createScope"
        else:
            document, operation = api.SCOPE_UPDATE, "updateScope"
            variables["id"] = identity
        data = await api.graphql(client, document, variables)
        return _json({"success": True, "scope": api.payload(data, operation, "scope")})
    except Exception as exc:
        return _error(exc)


def proxy_tools() -> list[Any]:
    return [list_requests, view_request, repeat_request, list_sitemap, view_sitemap_entry, scope_rules]
