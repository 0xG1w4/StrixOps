"""Exercise the proxy tools through the same FunctionTool boundary as agents.

Transport fixtures model captured traffic and the pinned Caido wire protocol;
no Caido instance, Docker engine, or external target is contacted here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from types import SimpleNamespace
from urllib.parse import parse_qsl

import pytest
from agents.tool_context import ToolContext

from strixops.tools import caido_api as api
from strixops.tools import proxy


def blob(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


class Transport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.threads = []

    def graphql(self, document, variables=None):
        self.calls.append((document, variables))
        self.threads.append(threading.get_ident())
        assert self.replies, "Unexpected extra GraphQL call"
        return self.replies.pop(0)


async def invoke(tool, client, **arguments):
    arguments_json = json.dumps(arguments)
    context = ToolContext(
        context=SimpleNamespace(services=SimpleNamespace(sandbox=SimpleNamespace(caido=client))),
        tool_name=tool.name,
        tool_call_id="proxy-regression",
        tool_arguments=arguments_json,
    )
    return json.loads(await tool.on_invoke_tool(context, arguments_json))


def request_node(identity="7", response=None):
    return {
        "id": identity,
        "host": "example.test",
        "port": 8080,
        "method": "GET",
        "path": "/api",
        "query": "q=1",
        "isTls": False,
        "createdAt": 0,
        "response": response,
    }


def request_page(nodes, *, more=False):
    return {
        "requests": {
            "edges": [{"cursor": "cursor-" + node["id"], "node": node} for node in nodes],
            "pageInfo": {
                "hasNextPage": more,
                "hasPreviousPage": False,
                "startCursor": "cursor-" + nodes[0]["id"] if nodes else None,
                "endCursor": "cursor-" + nodes[-1]["id"] if nodes else None,
            },
        }
    }


@pytest.mark.asyncio
async def test_list_uses_real_filter_cursor_order_and_keeps_unanswered_requests():
    transport = Transport([request_page([request_node()], more=True)])
    result = await invoke(
        proxy.list_requests,
        transport,
        httpql_filter="resp.code.gte:400",
        first=12,
        after="previous",
        sort_by="status_code",
        sort_order="asc",
        scope_id="s1",
    )
    assert result["success"] is True
    assert result["entries"][0]["request"]["port"] == 8080
    assert result["entries"][0]["request"]["is_tls"] is False
    assert result["entries"][0]["response"] is None
    assert result["entries"][0]["request"]["created_at"] == "1970-01-01T00:00:00+00:00"
    assert result["page_info"]["end_cursor"] == "cursor-7"
    assert transport.calls[0][1] == {
        "first": 12,
        "after": "previous",
        "scopeId": "s1",
        "filter": {"code": "resp.code.gte:400"},
        "order": {"by": "RESP_STATUS_CODE", "ordering": "ASC"},
    }
    assert "requests(" in transport.calls[0][0]
    assert "httpCalls" not in transport.calls[0][0]
    assert transport.threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_default_and_legacy_max_offset_do_not_shadow_builtin_or_invent_offsets():
    default = Transport([request_page([])])
    assert (await invoke(proxy.list_requests, default))["success"] is True
    assert default.calls[0][1]["first"] == 50
    transport = Transport(
        [request_page([request_node("1"), request_node("2")], more=True), request_page([request_node("3")])]
    )
    result = await invoke(proxy.list_requests, transport, max=1, offset=2)
    assert [entry["request"]["id"] for entry in result["entries"]] == ["3"]
    assert [call[1]["first"] for call in transport.calls] == [2, 1]
    assert transport.calls[1][1]["after"] == "cursor-2"
    assert all("offset" not in variables for _, variables in transport.calls)


@pytest.mark.asyncio
async def test_lazy_bootstrap_is_awaited_and_failures_are_tool_results():
    transport = Transport([request_page([])])

    class Handle:
        awaited = False

        async def get(self):
            await asyncio.sleep(0)
            self.awaited = True
            return transport

    handle = Handle()
    assert (await invoke(proxy.list_requests, handle))["success"] is True
    assert handle.awaited

    class FailedHandle:
        async def get(self):
            raise RuntimeError("Caido login failed")

    result = await invoke(proxy.list_requests, FailedHandle())
    assert result == {"success": False, "error": "Caido login failed"}
    assert (await invoke(proxy.list_sitemap, None))["success"] is False


@pytest.mark.asyncio
async def test_view_decodes_base64_and_paginates_or_searches_response_content():
    raw_request = b"GET /api HTTP/1.1\r\nHost: example.test\r\n\r\n"
    raw_response = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nalpha\nsecret=123\nomega"
    node = {**request_node(), "raw": blob(raw_request), "response": {"raw": blob(raw_response)}}
    transport = Transport([{"request": node}, {"request": node}, {"request": node}])
    page = await invoke(proxy.view_request, transport, request_id="7", part="response", page=2, page_size=3)
    assert page["content"] == "alpha\nsecret=123\nomega"
    assert page["total_lines"] == 6 and not page["has_more"]
    hits = await invoke(
        proxy.view_request, transport, request_id="7", part="response", search_pattern=r"secret=\d+"
    )
    assert hits["hits"][0]["match"] == "secret=123"
    assert "alpha" in hits["hits"][0]["before"]
    both = await invoke(proxy.view_request, transport, request_id="7", part="both")
    assert both["request"]["content"].startswith("GET /api HTTP/1.1")
    assert both["response"]["content"].startswith("HTTP/1.1 200 OK")


@pytest.mark.asyncio
async def test_view_bounds_regex_and_reports_missing_response_invalid_regex_and_bad_page():
    node = {**request_node(), "raw": blob(b"GET / HTTP/1.1\r\n\r\n" + b"match " * 25)}
    transport = Transport([{"request": node}] * 4)
    hits = await invoke(proxy.view_request, transport, request_id="7", search_pattern="match")
    assert len(hits["hits"]) == 20
    assert not (await invoke(proxy.view_request, transport, request_id="7", part="response"))["success"]
    assert not (await invoke(proxy.view_request, transport, request_id="7", search_pattern="["))["success"]
    assert not (await invoke(proxy.view_request, transport, request_id="7", page_size=0))["success"]


def captured(raw):
    return {**request_node(), "raw": blob(raw)}


def test_replay_merges_modifications_and_repairs_host_content_length_and_query():
    original = captured(
        b"POST /api?old=1 HTTP/1.1\r\nhOsT: example.test:8080\r\n"
        b"Authorization: old\r\nCookie: session=old; keep=yes\r\n"
        b"Content-Length: 3\r\nX-Tag: a\r\nX-Tag: b\r\n\r\nold"
    )
    connection, raw = api.prepare_replay(
        original,
        {
            "url": "https://new.test:9443/replaced?keep=1&dup=a&dup=b&blank=",
            "method": "PATCH",
            "path": "/v2",
            "params": {"q": "a b"},
            "headers": {"authorization": "new"},
            "header_X-Legacy": "retained",
            "cookies": {"session": "new", "extra": "yes"},
            "body": "測試",
        },
    )
    line, headers, body = api.split_http(raw)
    assert line.startswith("PATCH /v2?")
    assert parse_qsl(line.split(" ")[1].split("?", 1)[1], keep_blank_values=True) == [
        ("keep", "1"),
        ("dup", "a"),
        ("dup", "b"),
        ("blank", ""),
        ("q", "a b"),
    ]
    assert connection == {"host": "new.test", "port": 9443, "isTLS": True, "SNI": None}
    by_name = {}
    for key, value in headers:
        by_name.setdefault(key.lower(), []).append(value)
    assert by_name["host"] == ["new.test:9443"]
    assert by_name["content-length"] == ["6"]
    assert by_name["authorization"] == ["new"]
    assert by_name["x-tag"] == ["a", "b"]
    assert by_name["cookie"] == ["session=new; keep=yes; extra=yes"]
    assert body == "測試".encode()


def test_replay_preserves_binary_body_and_normalizes_chunked_framing():
    binary = b"\x00\xff ending\r\n "
    _, raw = api.prepare_replay(
        captured(b"POST /api?q=2 HTTP/1.1\r\nHost: virtual.test\r\nContent-Length: 99\r\n\r\n" + binary), {}
    )
    line, headers, body = api.split_http(raw)
    assert body == binary
    assert line == "POST /api?q=2 HTTP/1.1"
    assert ("Host", "virtual.test") in headers
    assert ("Content-Length", str(len(binary))) in headers
    _, raw = api.prepare_replay(
        captured(
            b"POST /api HTTP/1.1\r\nHost: example.test\r\n"
            b"Transfer-Encoding: chunked\r\nTrailer: checksum\r\n\r\n"
            b"3\r\none\r\n3\r\ntwo\r\n0\r\nchecksum: ignored\r\n\r\n"
        ),
        {},
    )
    _, headers, body = api.split_http(raw)
    assert body == b"onetwo"
    assert ("Content-Length", "6") in headers
    assert not any(key.lower() in ("transfer-encoding", "trailer") for key, _ in headers)


@pytest.mark.asyncio
async def test_repeat_dispatches_one_empty_session_and_waits_for_real_response(monkeypatch):
    monkeypatch.setattr(api, "REPLAY_POLL_INTERVAL", 0)
    original = captured(b"GET /api HTTP/1.1\r\nHost: example.test:8080\r\n\r\n")
    response = {
        "statusCode": 201,
        "length": 47,
        "roundtripTime": 17,
        "raw": blob(b"HTTP/1.1 201 Created\r\nX-Test: yes\r\n\r\nnew"),
    }
    transport = Transport(
        [
            {"request": original},
            {"createReplaySession": {"session": {"id": "s1"}}},
            {"startReplayTask": {"error": None, "task": {"id": "t1", "replayEntry": {"id": "r1"}}}},
            {"tasks": [{"id": "t1"}], "replayEntry": {"id": "r1", "error": None, "request": None}},
            {
                "tasks": [],
                "replayEntry": {"id": "r1", "error": None, "request": {"id": "8", "response": response}},
            },
        ]
    )
    result = await invoke(proxy.repeat_request, transport, request_id="7", modifications={"method": "POST"})
    assert result["success"] and result["status"] == "DONE"
    assert result["session_id"] == "s1" and result["request_id"] == "8"
    assert result["response"]["status_code"] == 201
    assert result["response"]["body"] == "new"
    assert result["response"]["length"] == 3
    assert result["response"]["headers"]["X-Test"] == "yes"
    assert transport.calls[1][1] == {"input": {}}
    send = transport.calls[2][1]
    assert send["sessionId"] == "s1"
    assert base64.b64decode(send["input"]["raw"]).startswith(b"POST /api")
    assert send["input"]["connection"]["port"] == 8080
    assert send["input"]["settings"]["updateContentLength"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "Connection refused"])
async def test_replay_no_response_is_never_reported_success(error):
    transport = Transport(
        [
            {"createReplaySession": {"session": {"id": "s1"}}},
            {"startReplayTask": {"task": {"id": "t1", "replayEntry": {"id": "r1"}}}},
            {"tasks": [], "replayEntry": {"error": error, "request": None}},
        ]
    )
    result = await api.send_replay(transport, {}, b"GET / HTTP/1.1\r\n\r\n")
    assert not result["success"] and result["status"] == "ERROR"
    assert result["error"] and result["response"] is None


@pytest.mark.asyncio
async def test_replay_timeout_does_not_dispatch_twice(monkeypatch):
    monkeypatch.setattr(api, "REPLAY_TIMEOUT", 0.02)
    monkeypatch.setattr(api, "REPLAY_POLL_INTERVAL", 1)
    transport = Transport(
        [
            {"createReplaySession": {"session": {"id": "s1"}}},
            {"startReplayTask": {"task": {"id": "t1", "replayEntry": {"id": "r1"}}}},
            {"tasks": [{"id": "t1"}], "replayEntry": {"request": None}},
        ]
    )
    result = await api.send_replay(transport, {}, b"GET / HTTP/1.1\r\n\r\n")
    assert not result["success"]
    assert "may still be running" in result["error"]
    assert len(transport.calls) == 3


@pytest.mark.asyncio
async def test_nested_mutation_error_does_not_look_like_success():
    original = captured(b"GET /api HTTP/1.1\r\nHost: example.test\r\n\r\n")
    transport = Transport(
        [
            {"request": original},
            {"createReplaySession": {"session": {"id": "s1"}}},
            {
                "startReplayTask": {
                    "error": {"__typename": "PermissionDeniedUserError", "code": "DENIED"},
                    "task": None,
                }
            },
        ]
    )
    result = await invoke(proxy.repeat_request, transport, request_id="7")
    assert not result["success"] and "DENIED" in result["error"]
    assert len(transport.calls) == 3


def sitemap_entry(identity):
    return {
        "id": str(identity),
        "kind": "DOMAIN",
        "label": "example.test",
        "hasDescendants": True,
        "metadata": {"isTls": False, "port": 8080},
        "request": None,
    }


@pytest.mark.asyncio
async def test_sitemap_scope_subtree_and_client_side_page_have_real_tree_metadata():
    connection = {"edges": [{"node": sitemap_entry(i)} for i in range(31)], "count": {"value": 31}}
    transport = Transport([{"sitemapRootEntries": connection}, {"sitemapDescendantEntries": connection}])
    roots = await invoke(proxy.list_sitemap, transport, scope_id="scope1", page=2)
    assert len(roots["entries"]) == 1 and roots["total_count"] == 31
    assert roots["entries"][0]["metadata"] == {"is_tls": False, "port": 8080}
    assert roots["entries"][0]["has_descendants"] and not roots["has_more"]
    descendants = await invoke(proxy.list_sitemap, transport, parent_id="10", depth="ALL", page=1)
    assert len(descendants["entries"]) == 30 and descendants["has_more"]
    assert transport.calls[0][1] == {"scopeId": "scope1"}
    assert transport.calls[1][1] == {"parentId": "10", "depth": "ALL"}


@pytest.mark.asyncio
async def test_sitemap_detail_reports_total_and_recent_request_summaries():
    node = {
        **sitemap_entry("1"),
        "request": {"method": "GET", "path": "/api", "response": {"statusCode": 200, "length": 8}},
        "requests": {
            "edges": [
                {"node": {"method": "POST", "path": "/api", "response": {"statusCode": 403, "length": 3}}}
            ],
            "count": {"value": 35},
        },
    }
    transport = Transport([{"sitemapEntry": node}])
    result = await invoke(proxy.view_sitemap_entry, transport, entry_id="1")
    assert result["entry"]["request"]["response"]["status_code"] == 200
    assert result["entry"]["related_requests"]["total_count"] == 35
    assert result["entry"]["related_requests"]["requests"][0]["status_code"] == 403
    assert "requests(first: 30" in transport.calls[0][0]


@pytest.mark.asyncio
async def test_scope_full_crud_and_names_match_payloads():
    scope = {
        "id": "scope1",
        "name": "Target",
        "allowlist": ["*.example.test"],
        "denylist": [],
        "indexed": True,
    }
    transport = Transport(
        [
            {"scopes": [scope]},
            {"scope": scope},
            {"createScope": {"error": None, "scope": scope}},
            {"updateScope": {"error": None, "scope": {**scope, "name": "Updated"}}},
            {"deleteScope": {"deletedId": "scope1"}},
        ]
    )
    assert (await invoke(proxy.scope_rules, transport, action="list"))["scopes"] == [scope]
    assert (await invoke(proxy.scope_rules, transport, action="get", scope_id="scope1"))["scope"] == scope
    assert (
        await invoke(
            proxy.scope_rules, transport, action="create", scope_name="Target", allowlist=["*.example.test"]
        )
    )["scope"] == scope
    assert transport.calls[2][1] == {
        "input": {"name": "Target", "allowlist": ["*.example.test"], "denylist": []}
    }
    assert (
        await invoke(
            proxy.scope_rules,
            transport,
            action="update",
            scope_id="scope1",
            scope_name="Updated",
            denylist=["*.cdn.test"],
        )
    )["scope"]["name"] == "Updated"
    assert transport.calls[3][1]["id"] == "scope1"
    assert transport.calls[3][1]["input"]["denylist"] == ["*.cdn.test"]
    result = await invoke(proxy.scope_rules, transport, action="delete", scope_id="scope1")
    assert result["success"] and result["deleted"] == "scope1"


@pytest.mark.asyncio
async def test_scope_preserves_default_create_and_rejects_invalid_mutations():
    transport = Transport(
        [
            {"createScope": {"scope": {"id": "s1", "name": "strixops-scope"}}},
            {
                "createScope": {
                    "scope": None,
                    "error": {"__typename": "InvalidGlobTermsUserError", "code": "INVALID_GLOB"},
                }
            },
        ]
    )
    assert (await invoke(proxy.scope_rules, transport, action="create"))["success"]
    assert transport.calls[0][1]["input"]["name"] == "strixops-scope"
    assert not (await invoke(proxy.scope_rules, transport, action="delete"))["success"]
    assert not (await invoke(proxy.scope_rules, transport, action="update", scope_id="s1"))["success"]
    result = await invoke(proxy.scope_rules, transport, action="create", allowlist=["["])
    assert not result["success"] and "INVALID_GLOB" in result["error"]
    assert len(transport.calls) == 2
