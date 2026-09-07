"""Caido bootstrap and pinned SDK container integration without a live daemon."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import docker
import pytest
from agents.sandbox.sandboxes import docker as sdk_docker

from strixops.runtime import caido, sandbox


@pytest.fixture()
def client(monkeypatch):
    http = Mock()
    monkeypatch.setattr(caido._requests, "Session", Mock(return_value=http))
    return caido.CaidoClient("http://127.0.0.1:49123", "test-token"), http


def _response(payload):
    return Mock(json=Mock(return_value=payload))


def test_project_bootstrap_uses_nested_graphql_contract(client):
    connection, http = client
    http.post.side_effect = [
        _response({"data": {"createProject": {"project": {"id": "p1", "name": "strixops"}}}}),
        _response({"data": {"selectProject": {"currentProject": {"project": {"id": "p1"}}}}}),
    ]

    assert connection.create_and_select_project() == "p1"
    assert connection._project_id == "p1"
    creation, selection = http.post.call_args_list
    assert creation.args == ("http://127.0.0.1:49123/graphql",)
    assert "createProject(input: $input)" in creation.kwargs["json"]["query"]
    assert creation.kwargs["json"]["variables"] == {"input": {"name": "strixops", "temporary": True}}
    assert "currentProject { project { id } }" in selection.kwargs["json"]["query"]
    assert selection.kwargs["json"]["variables"] == {"id": "p1"}
    assert http.trust_env is False
    assert http.headers.update.call_args.args[0]["Authorization"] == "Bearer test-token"


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"__typename": "ProjectError"}, "currentProject": None},
        {"error": None, "currentProject": {"project": {"id": "different-project"}}},
    ],
)
def test_project_selection_failure_is_not_accepted(client, payload):
    connection, http = client
    http.post.side_effect = [
        _response({"data": {"createProject": {"project": {"id": "p1"}}}}),
        _response({"data": {"selectProject": payload}}),
    ]

    with pytest.raises(RuntimeError, match="selectProject failed"):
        connection.create_and_select_project()

    assert connection._project_id is None


def test_embedded_project_creation_error_stops_selection(client):
    connection, http = client
    http.post.return_value = _response(
        {"data": {"createProject": {"project": None, "error": {"__typename": "ProjectError"}}}}
    )

    with pytest.raises(RuntimeError, match="createProject returned no id"):
        connection.create_and_select_project()

    http.post.assert_called_once()
    assert connection._project_id is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"errors": [{"message": "invalid query"}], "data": None}, "GraphQL error"),
        ({"data": None}, "returned no data"),
    ],
)
def test_invalid_graphql_result_raises(client, payload, message):
    connection, http = client
    http.post.return_value = _response(payload)

    with pytest.raises(RuntimeError, match=message):
        connection.graphql("query { currentProject { project { id } } }")


def test_closed_client_cannot_start_another_http_request(client):
    connection, http = client
    connection.close()

    with pytest.raises(RuntimeError, match="client is closed"):
        connection.graphql("query { currentProject { project { id } } }")

    http.close.assert_called_once()
    http.post.assert_not_called()


async def test_guest_login_retries_missing_and_null_tokens(monkeypatch):
    payloads = [
        {},
        {"data": None},
        {"data": {"loginAsGuest": None}},
        {"data": {"loginAsGuest": {"token": None}}},
        {"data": {"loginAsGuest": {"token": {"accessToken": "guest-token"}}}},
    ]
    session = SimpleNamespace(
        exec=AsyncMock(
            side_effect=[Mock(ok=Mock(return_value=True), stdout=json.dumps(payload)) for payload in payloads]
        )
    )
    sleep = AsyncMock()
    monkeypatch.setattr(caido.asyncio, "sleep", sleep)

    assert await caido.login_as_guest(session, attempts=5) == "guest-token"
    assert session.exec.await_count == 5
    assert sleep.await_count == 4
    args = session.exec.call_args.args
    assert args[args.index("--noproxy") + 1] == "*"
    assert args[-1] == "http://127.0.0.1:48080/graphql"


async def test_guest_login_exhaustion_reports_failure(monkeypatch):
    session = SimpleNamespace(
        exec=AsyncMock(return_value=Mock(ok=Mock(return_value=True), stdout='{"data": null}'))
    )
    monkeypatch.setattr(caido.asyncio, "sleep", AsyncMock())

    with pytest.raises(RuntimeError, match="failed after 2 attempts: .*no access token"):
        await caido.login_as_guest(session, attempts=2)


async def test_bootstrap_cancellation_waits_for_setup_before_close(monkeypatch):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    events = []

    def setup():
        loop.call_soon_threadsafe(started.set)
        if not release.wait(timeout=3):
            raise RuntimeError("test did not release project setup")
        events.append("setup-finished")

    connection = SimpleNamespace(
        create_and_select_project=setup,
        close=lambda: events.append("closed"),
    )
    monkeypatch.setattr(caido, "login_as_guest", AsyncMock(return_value="guest-token"))
    monkeypatch.setattr(caido, "CaidoClient", Mock(return_value=connection))
    task = asyncio.create_task(caido.bootstrap_caido(object(), host_url="http://127.0.0.1:49123"))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert events == []
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    assert events == ["setup-finished", "closed"]


async def test_bootstrap_failure_closes_client(monkeypatch):
    connection = Mock()
    connection.create_and_select_project.side_effect = RuntimeError("project unavailable")
    monkeypatch.setattr(caido, "login_as_guest", AsyncMock(return_value="guest-token"))
    monkeypatch.setattr(caido, "CaidoClient", Mock(return_value=connection))

    with pytest.raises(RuntimeError, match="project unavailable"):
        await caido.bootstrap_caido(object(), host_url="http://127.0.0.1:49123")

    connection.close.assert_called_once()


async def test_cancelled_get_does_not_cancel_shared_bootstrap():
    release = asyncio.Event()
    connection = Mock()

    async def initialize():
        await release.wait()
        return connection

    bootstrap = asyncio.create_task(initialize())
    handle = caido.CaidoBootstrapHandle(bootstrap)
    waiter = asyncio.create_task(handle.get())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not bootstrap.done()

    release.set()
    assert await handle.get() is connection
    await handle.aclose()
    connection.close.assert_called_once()


@pytest.mark.parametrize("use_caido", [False, True])
@pytest.mark.parametrize("with_workspace", [False, True])
@pytest.mark.parametrize("scan_type", ["web", "internal"])
async def test_sandbox_sdk_creation_preserves_required_entrypoint_and_options(
    monkeypatch, tmp_path, use_caido, with_workspace, scan_type
):
    docker_client = Mock()
    session = SimpleNamespace(
        start=AsyncMock(),
        resolve_exposed_port=AsyncMock(return_value=SimpleNamespace(tls=False, host="127.0.0.1", port=49123)),
    )
    sdk_mount = docker.types.Mount(target="/sdk-data", source="sdk-volume", type="volume")
    monkeypatch.setattr(docker, "from_env", Mock(return_value=docker_client))
    ensure_image = Mock()
    monkeypatch.setattr(sandbox, "_ensure_image", ensure_image)
    monkeypatch.setattr(sdk_docker, "_build_docker_volume_mounts", Mock(return_value=[sdk_mount]))

    async def create(client, *, options, manifest):
        # Exercise the pinned SDK's actual kwargs generation, replacing only
        # Docker execution and the unrelated session/transport lifecycle.
        await client._create_container(options.image, manifest=manifest, exposed_ports=options.exposed_ports)
        return session

    delete = AsyncMock()
    monkeypatch.setattr(sdk_docker.DockerSandboxClient, "create", create)
    monkeypatch.setattr(sdk_docker.DockerSandboxClient, "delete", delete)
    connection = Mock()
    initialize = AsyncMock(return_value=connection)
    monkeypatch.setattr(caido, "bootstrap_caido", initialize)

    bundle = await sandbox.create_sandbox_session(
        image="test-sandbox",
        scan_type=scan_type,
        socks5_proxy="socks5://proxy.internal:1080",
        gsocket_key="test-tunnel-key",
        use_caido=use_caido,
        host_workspace_dir=str(tmp_path / "workspace") if with_workspace else "",
    )
    try:
        ensure_image.assert_called_once_with("test-sandbox")
        kwargs = docker_client.containers.create.call_args.kwargs
        assert kwargs["image"] == "test-sandbox"
        assert kwargs["detach"] is True
        assert kwargs["environment"]["PYTHONUNBUFFERED"] == "1"
        if scan_type == "internal":
            assert kwargs["environment"]["SOCKS5_PROXY"] == "socks5://proxy.internal:1080"
            assert kwargs["environment"]["GSOCKET_KEY"] == "test-tunnel-key"
        else:
            assert "SOCKS5_PROXY" not in kwargs["environment"]
            assert "GSOCKET_KEY" not in kwargs["environment"]
        assert kwargs["mounts"][-1] == sdk_mount
        if with_workspace:
            assert len(kwargs["mounts"]) == 2
            assert kwargs["mounts"][0]["Target"] == "/workspace"
            assert kwargs["mounts"][0]["Source"] == str(tmp_path / "workspace")
        else:
            assert kwargs["mounts"] == [sdk_mount]
        if use_caido:
            assert "entrypoint" not in kwargs
            assert kwargs["command"] == ["tail", "-f", "/dev/null"]
            assert kwargs["ports"] == {"48080/tcp": ("127.0.0.1", None)}
            assert kwargs["environment"]["https_proxy"] == "http://127.0.0.1:48080"
            assert kwargs["environment"]["NO_PROXY"] == "localhost,127.0.0.1"
            assert await bundle.caido.get() is connection
            initialize.assert_awaited_once_with(session, host_url="http://127.0.0.1:49123")
        else:
            assert kwargs["entrypoint"] == ["tail"]
            assert kwargs["command"] == ["-f", "/dev/null"]
            assert "ports" not in kwargs
            assert "https_proxy" not in kwargs["environment"]
            assert bundle.caido is None
            initialize.assert_not_called()
        session.start.assert_awaited_once()
    finally:
        await bundle.teardown()

    delete.assert_awaited_once_with(session)
    if use_caido:
        connection.close.assert_called_once()


def _diagnostic_script():
    namespace = {"__name__": "diagnostic_test"}
    exec(caido._DIAGNOSTIC_SCRIPT, namespace)
    return namespace


def test_diagnostic_log_tail_is_bounded_and_redacts_credentials(tmp_path, monkeypatch):
    namespace = _diagnostic_script()
    log = tmp_path / "caido_startup.log"
    log.write_text(
        "old data\n" * 3000
        + "ERROR connection refused\nAuthorization: Bearer guest-token\n"
        + "api_key=test-key\nrequest https://target.test/path?token=secret\n"
        + "opaque super-secret-from-env\n",
        encoding="utf-8",
    )
    namespace["LOG_PATH"] = str(log)
    monkeypatch.setenv("TEST_DIAGNOSTIC_SECRET", "super-secret-from-env")

    result = namespace["startup_log"]()

    assert result["status"] == "ok"
    assert result["truncated"] is True
    assert "connection refused" in result["tail"]
    for secret in ("guest-token", "test-key", "target.test", "super-secret-from-env"):
        assert secret not in result["tail"]
    assert len(result["tail"]) <= 17000


def test_diagnostic_missing_log_and_symlinks_are_not_followed(tmp_path):
    namespace = _diagnostic_script()
    path = tmp_path / "caido_startup.log"
    namespace["LOG_PATH"] = str(path)
    assert namespace["startup_log"]()["error_type"] == "FileNotFoundError"
    secret = tmp_path / "secret"
    secret.write_text("sensitive unrelated file")
    path.symlink_to(secret)
    result = namespace["startup_log"]()
    assert result["status"] == "unavailable"
    assert "sensitive" not in json.dumps(result)


@pytest.mark.parametrize("title", ["Caido", "Target application"])
def test_502_summary_preserves_error_markers_without_traffic_contents(title):
    namespace = _diagnostic_script()
    raw = (
        f"HTTP/1.1 502 Bad Gateway\r\nSet-Cookie: secret-cookie\r\n\r\n"
        f"<html><title>{title}</title><p>Error trying to connect: connection refused</p>"
        "<p>https://target.test/private?token=secret-token</p></html>"
    ).encode()

    result = namespace["summarize_response"](
        {"id": "r1", "response": {"statusCode": 502, "raw": base64.b64encode(raw).decode()}}
    )

    assert result["request_id"] == "r1"
    assert result["status_code"] == 502
    if title == "Caido":
        assert result["page_title"] == "Caido"
        assert "connection refused" in result["error_markers"]
    else:
        assert result["detail_status"] == "non_caido_response_omitted"
        assert "error_markers" not in result
    for secret in ("secret-cookie", "secret-token", "target.test", "private"):
        assert secret not in json.dumps(result)


def test_diagnostic_graphql_uses_only_loopback_and_omits_guest_token(monkeypatch):
    namespace = _diagnostic_script()
    responses = [
        {"data": {"loginAsGuest": {"token": {"accessToken": "test-private-token"}}}},
        {"data": {"requests": {"edges": []}}},
    ]
    requests = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            assert limit == 524289
            return json.dumps(self.payload).encode()

    def open_local(request, timeout):
        requests.append(request)
        assert request.full_url == "http://127.0.0.1:48080/graphql"
        assert timeout == 1.0
        return Response(responses.pop(0))

    opener = SimpleNamespace(open=open_local)
    monkeypatch.setattr(namespace["urllib"].request, "build_opener", Mock(return_value=opener))

    result = namespace["captured_errors"]()

    assert result["status"] == "ok"
    assert result["sample_limit"] == 3
    assert result["samples"] == []
    assert "empty samples do not rule out proxy errors" in result["coverage_note"]
    assert len(requests) == 2
    query = json.loads(requests[1].data)["query"]
    assert 'filter: {code: "resp.code.eq:502"}' in query
    assert "order: {by: CREATED_AT, ordering: DESC}" in query
    assert "test-private-token" not in json.dumps(result)


async def test_failed_teardown_persists_diagnostics_before_deleting_sandbox(tmp_path):
    order = []

    async def execute(*args, **kwargs):
        assert args[:2] == ("python3", "-c")
        assert kwargs["timeout"] == 4
        order.append(args[3])
        return Mock(ok=Mock(return_value=True), stdout=json.dumps({"status": "ok"}))

    async def delete(session):
        assert (tmp_path / "caido.json").exists()
        order.append("deleted")

    bundle = sandbox.SandboxBundle(
        client=SimpleNamespace(delete=delete),
        session=SimpleNamespace(exec=execute),
        caido=SimpleNamespace(aclose=AsyncMock()),
    )
    await bundle.teardown(diagnostics_dir=tmp_path)

    report = json.loads((tmp_path / "caido.json").read_text())
    assert report["startup_log"]["status"] == "ok"
    assert report["captured_http_502"]["status"] == "ok"
    assert (tmp_path / "caido.json").stat().st_mode & 0o777 == 0o600
    assert order[-1] == "deleted"


async def test_diagnostic_timeout_retains_log_and_does_not_prevent_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(caido, "DIAGNOSTIC_TIMEOUT", 0.01)

    async def execute(*args, **kwargs):
        if args[3] == "captured_http_502":
            await asyncio.Event().wait()
        return Mock(ok=Mock(return_value=True), stdout='{"status":"ok","tail":"ready"}')

    delete = AsyncMock()
    bundle = sandbox.SandboxBundle(
        client=SimpleNamespace(delete=delete),
        session=SimpleNamespace(exec=execute),
        caido=SimpleNamespace(aclose=AsyncMock()),
    )
    await asyncio.wait_for(bundle.teardown(diagnostics_dir=tmp_path), timeout=0.5)

    report = json.loads((tmp_path / "caido.json").read_text())
    assert report["startup_log"]["tail"] == "ready"
    assert report["captured_http_502"] == {"status": "unavailable", "error_type": "TimeoutError"}
    delete.assert_awaited_once()


@pytest.mark.parametrize("has_caido", [False, True])
async def test_diagnostics_are_skipped_without_failure_path_or_caido(tmp_path, has_caido):
    execute, delete = AsyncMock(), AsyncMock()
    bundle = sandbox.SandboxBundle(
        client=SimpleNamespace(delete=delete),
        session=SimpleNamespace(exec=execute),
        caido=SimpleNamespace(aclose=AsyncMock()) if has_caido else None,
    )
    await bundle.teardown(diagnostics_dir=None if has_caido else tmp_path)
    execute.assert_not_called()
    delete.assert_awaited_once()
    assert not (tmp_path / "caido.json").exists()


async def test_diagnostic_exec_and_write_failures_never_block_teardown(tmp_path):
    destination = tmp_path / "not-a-directory"
    destination.write_text("existing file")
    delete = AsyncMock()
    bundle = sandbox.SandboxBundle(
        client=SimpleNamespace(delete=delete),
        session=SimpleNamespace(exec=AsyncMock(side_effect=RuntimeError("secret-must-not-be-logged"))),
        caido=SimpleNamespace(aclose=AsyncMock()),
    )
    await bundle.teardown(diagnostics_dir=destination)
    delete.assert_awaited_once()
    assert destination.read_text() == "existing file"
