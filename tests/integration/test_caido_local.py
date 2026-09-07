"""Opt-in Caido protocol check against a container-local HTTP echo server.

Set STRIXOPS_CAIDO_TEST=1 to run. Requires the image already present locally;
this test never pulls images, publishes ports, mounts host paths, or contacts
external targets. Docker's ``none`` network confines Caido and its target to
one disposable container. GraphQL control requests use ``docker exec curl``.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import os
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import docker
import pytest
import requests
from agents.tool_context import ToolContext

from strixops.runtime.caido import CAIDO_CONTAINER_URL, CaidoClient, login_as_guest
from strixops.runtime.sandbox import default_image
from strixops.tools import proxy

pytestmark = pytest.mark.skipif(
    os.environ.get("STRIXOPS_CAIDO_TEST") != "1",
    reason="set STRIXOPS_CAIDO_TEST=1 (requires Docker + a locally installed sandbox image)",
)

_GRAPHQL_URL = f"{CAIDO_CONTAINER_URL}/graphql"
_TARGET = "http://127.0.0.1:18081"
_FILTER = 'req.host.eq:"127.0.0.1" AND req.port.eq:18081'
_SERVER = r"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Echo(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.dumps({
            "method": self.command,
            "path": self.path,
            "body": body.decode("utf-8"),
            "headers": {k.lower(): v for k, v in self.headers.items()},
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = reply
    do_POST = reply
    do_PATCH = reply

ThreadingHTTPServer(("127.0.0.1", 18081), Echo).serve_forever()
"""


@pytest.fixture()
def isolated_container() -> Iterator[Any]:
    client = docker.from_env(timeout=40)
    container = None
    image_name = default_image()
    try:
        try:
            image = client.images.get(image_name)
        except docker.errors.ImageNotFound:
            pytest.skip(f"required local image is missing: {image_name}; no image pull attempted")
        options = {
            "image": image.id,
            "name": f"strixops-caido-local-{uuid4().hex}",
            # Preserve the image entrypoint: it starts Caido and installs its CA.
            "command": ["tail", "-f", "/dev/null"],
            "detach": True,
            "network_mode": "none",
            "ports": {},
            "volumes": {},
            "environment": {
                "http_proxy": "",
                "https_proxy": "",
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "ALL_PROXY": "",
                "all_proxy": "",
                "NO_PROXY": "*",
                "no_proxy": "*",
            },
            "labels": {"strixops.test": "caido-local"},
        }
        assert options["network_mode"] == "none"
        assert not options["ports"] and not options["volumes"]
        assert "entrypoint" not in options
        container = client.containers.create(**options)
        container.start()
        container.reload()
        assert container.attrs["HostConfig"]["NetworkMode"] == "none"
        assert not container.attrs["HostConfig"].get("PortBindings")
        assert not container.attrs["HostConfig"].get("Binds")
        assert not container.attrs.get("Mounts")
        container.exec_run(["python3", "-u", "-c", _SERVER], detach=True)
        yield container
    finally:
        try:
            if container is not None:
                # Only the container created above belongs to this fixture.
                container.remove(force=True, v=True)
        finally:
            client.close()


class _LoginSession:
    def __init__(self, container: Any) -> None:
        self.container = container

    async def exec(self, *args: str, timeout: int) -> Any:
        assert args[0] == "curl" and args[-1] == _GRAPHQL_URL
        assert timeout <= 15
        result = await asyncio.to_thread(self.container.exec_run, list(args), demux=True)
        stdout, stderr = result.output
        return SimpleNamespace(
            stdout=stdout or b"",
            stderr=stderr or b"",
            exit_code=result.exit_code,
            ok=lambda: result.exit_code == 0,
        )


class _CurlResponse:
    def __init__(self, body: bytes, status: int) -> None:
        self.body = body
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise requests.HTTPError(f"container-local Caido returned HTTP {self.status}")

    def json(self) -> dict[str, Any]:
        return jsonlib.loads(self.body)


class _ExecTransport:
    """requests.Session-compatible control transport, with no host HTTP I/O."""

    def __init__(self, container: Any, headers: dict[str, str]) -> None:
        self.container = container
        self.headers = dict(headers)

    def post(self, url: str, *, json: dict[str, Any], timeout: int) -> _CurlResponse:
        assert url == _GRAPHQL_URL
        command = [
            "curl",
            "-sS",
            "--max-time",
            str(timeout),
            "--noproxy",
            "*",
            "-X",
            "POST",
            "-w",
            "\n%{http_code}",
        ]
        for name, value in self.headers.items():
            command.extend(["-H", f"{name}: {value}"])
        command.extend(["--data-binary", jsonlib.dumps(json), _GRAPHQL_URL])
        result = self.container.exec_run(command, demux=True)
        if result.exit_code:
            # Do not print control command arguments, headers, or guest tokens.
            raise RuntimeError(f"container-local GraphQL curl exited {result.exit_code}")
        stdout, _ = result.output
        body, _, status = (stdout or b"").rpartition(b"\n")
        return _CurlResponse(body, int(status))

    def close(self) -> None:
        pass


async def _tool(client: CaidoClient, name: str, **args: Any) -> dict[str, Any]:
    arguments = jsonlib.dumps(args)
    context = ToolContext(
        context=SimpleNamespace(services=SimpleNamespace(sandbox=SimpleNamespace(caido=client))),
        tool_name=name,
        tool_call_id=f"local-{uuid4().hex}",
        tool_arguments=arguments,
    )
    output = await getattr(proxy, name).on_invoke_tool(context, arguments)
    payload = jsonlib.loads(output)
    assert payload.get("success") is True, f"{name}: {payload}"
    return payload


async def _capture(container: Any, path: str, *, body: str | None = None) -> dict[str, Any]:
    assert path.startswith("/api/")
    command = [
        "curl",
        "-fsS",
        "--max-time",
        "10",
        "--noproxy",
        "",
        "--proxy",
        CAIDO_CONTAINER_URL,
        "-H",
        "X-Proxy-Test: captured",
    ]
    if body is not None:
        command.extend(["--data-binary", body])
    command.append(f"{_TARGET}{path}")
    result = await asyncio.to_thread(container.exec_run, command, demux=True)
    assert result.exit_code == 0, f"local fixture capture failed: exit {result.exit_code}"
    return jsonlib.loads(result.output[0])


async def _wait_for_fixture(container: Any) -> None:
    for _ in range(30):
        result = await asyncio.to_thread(
            container.exec_run,
            ["curl", "-fsS", "--max-time", "1", "--noproxy", "*", f"{_TARGET}/ready"],
        )
        if result.exit_code == 0:
            return
        await asyncio.sleep(0.2)
    pytest.fail("container-local HTTP fixture did not become ready")


async def test_caido_protocol_with_only_container_local_traffic(isolated_container):
    container = isolated_container
    await _wait_for_fixture(container)
    token = await login_as_guest(_LoginSession(container))
    client = CaidoClient(CAIDO_CONTAINER_URL, token)
    transport = _ExecTransport(container, dict(client._session.headers))
    client._session.close()
    client._session = transport
    try:
        project_id = await asyncio.to_thread(client.create_and_select_project)
        assert project_id and client._project_id == project_id

        captured_body = "captured café ✓\n"
        captured = await _capture(container, "/api/one?original=1", body=captured_body)
        assert captured["body"] == captured_body
        assert captured["method"] == "POST"
        await _capture(container, "/api/two")
        await _capture(container, "/api/three")

        for _ in range(30):
            listing = await _tool(client, "list_requests", httpql_filter=_FILTER, first=50)
            if len(listing["entries"]) >= 3:
                break
            await asyncio.sleep(0.2)
        assert len(listing["entries"]) == 3
        request_ids = {entry["request"]["id"] for entry in listing["entries"]}
        assert all(entry["request"]["host"] == "127.0.0.1" for entry in listing["entries"])
        assert all(entry["request"]["port"] == 18081 for entry in listing["entries"])
        assert all(entry["response"]["status_code"] == 200 for entry in listing["entries"])

        filtered = await _tool(
            client,
            "list_requests",
            httpql_filter=f'{_FILTER} AND req.method.eq:"POST"',
        )
        assert len(filtered["entries"]) == 1
        request_id = filtered["entries"][0]["request"]["id"]
        page_one = await _tool(client, "list_requests", httpql_filter=_FILTER, first=1, sort_order="asc")
        assert page_one["page_info"]["has_next_page"] is True
        page_two = await _tool(
            client,
            "list_requests",
            httpql_filter=_FILTER,
            first=1,
            sort_order="asc",
            after=page_one["page_info"]["end_cursor"],
        )
        assert page_one["entries"][0]["request"]["id"] != page_two["entries"][0]["request"]["id"]

        raw = await _tool(client, "view_request", request_id=request_id, part="request", page_size=100)
        assert "POST /api/one?original=1 HTTP/1.1" in raw["content"]
        assert captured_body.rstrip("\n") in raw["content"]
        first_lines = await _tool(client, "view_request", request_id=request_id, page_size=2)
        next_lines = await _tool(client, "view_request", request_id=request_id, page=2, page_size=2)
        assert first_lines["has_more"] is True
        assert first_lines["content"] != next_lines["content"]
        hits = await _tool(client, "view_request", request_id=request_id, search_pattern="captured")
        assert any(hit["match"] == "captured" for hit in hits["hits"])
        response = await _tool(client, "view_request", request_id=request_id, part="response")
        assert "200" in response["content"] and "captured" in response["content"]

        replay_body = "modified café ✓\n\n"
        replay = await _tool(
            client,
            "repeat_request",
            request_id=request_id,
            modifications={
                "url": f"{_TARGET}/api/replayed",
                "method": "PATCH",
                "params": {"changed": "yes"},
                "headers": {"X-Proxy-Test": "replayed"},
                "body": replay_body,
                "cookies": {"local-test": "yes"},
            },
        )
        assert replay["status"] == "DONE"
        assert replay["response"]["status_code"] == 200
        echoed = jsonlib.loads(replay["response"]["body"])
        assert echoed["method"] == "PATCH"
        assert echoed["path"] == "/api/replayed?changed=yes"
        assert echoed["body"] == replay_body
        assert echoed["headers"]["x-proxy-test"] == "replayed"
        assert "local-test=yes" in echoed["headers"]["cookie"]
        assert int(echoed["headers"]["content-length"]) == len(replay_body.encode("utf-8"))

        for _ in range(30):
            roots = await _tool(client, "list_sitemap")
            if roots["entries"]:
                break
            await asyncio.sleep(0.2)
        root = next(entry for entry in roots["entries"] if entry["kind"] == "DOMAIN")
        children = await _tool(client, "list_sitemap", parent_id=root["id"], depth="DIRECT")
        assert children["entries"]
        descendants = await _tool(client, "list_sitemap", parent_id=root["id"], depth="ALL")
        assert len(descendants["entries"]) >= len(children["entries"])
        detail = await _tool(client, "view_sitemap_entry", entry_id=root["id"])
        assert detail["entry"]["id"] == root["id"]
        assert detail["entry"]["related_requests"]["total_count"] >= 3

        created = await _tool(
            client,
            "scope_rules",
            action="create",
            scope_name="container-local",
            allowlist=["127.0.0.1"],
            denylist=[],
        )
        scope_id = created["scope"]["id"]
        scopes = await _tool(client, "scope_rules", action="list")
        assert scope_id in {scope["id"] for scope in scopes["scopes"]}
        scope = await _tool(client, "scope_rules", action="get", scope_id=scope_id)
        assert scope["scope"]["id"] == scope_id
        for _ in range(30):
            scoped = await _tool(client, "list_requests", scope_id=scope_id, httpql_filter=_FILTER)
            scoped_ids = {entry["request"]["id"] for entry in scoped["entries"]}
            if request_ids <= scoped_ids:
                break
            await asyncio.sleep(0.2)
        assert request_ids <= scoped_ids
        scoped_roots = await _tool(client, "list_sitemap", scope_id=scope_id)
        assert root["id"] in {entry["id"] for entry in scoped_roots["entries"]}
        updated = await _tool(
            client,
            "scope_rules",
            action="update",
            scope_id=scope_id,
            scope_name="local-renamed",
            allowlist=["127.0.0.1"],
            denylist=["unmatched.invalid"],
        )
        assert updated["scope"]["id"] == scope_id
        assert updated["scope"]["name"] == "local-renamed"
        await _tool(client, "scope_rules", action="delete", scope_id=scope_id)
        remaining = await _tool(client, "scope_rules", action="list")
        assert scope_id not in {scope["id"] for scope in remaining["scopes"]}
    finally:
        client.close()
