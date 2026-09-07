"""Passive proxy telemetry: coverage, privacy, explicit bindings, and cached reads."""

from __future__ import annotations

import contextlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import docker
import pytest

from strixops.console import proxy_status as status

NOW = datetime(2026, 9, 5, 14, 0, tzinfo=UTC)
CONTAINER = "a" * 64


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    status._CACHE.clear()
    status._LOCKS.clear()
    monkeypatch.setattr(status.docker, "from_env", Mock(side_effect=AssertionError("unexpected Docker")))


def _collector():
    namespace = {"__name__": "test_collector"}
    exec(status._COLLECTOR, namespace)
    return namespace


def _sample(**updates):
    value = {
        "state": "healthy",
        "reason": "traffic_captured",
        "checked_at": NOW.isoformat(),
        "last_activity_at": (NOW - timedelta(seconds=17)).isoformat(),
        "checks": {"listener": True, "api": True, "capture": True},
        "metrics": {
            "captured_total": 12,
            "requests_window": 4,
            "proxy_errors_window": 0,
            "timeouts_window": 0,
        },
        "series": [{"timestamp": (NOW - timedelta(seconds=300)).isoformat(), "requests": 4, "errors": 0}],
        "recent_errors": [],
        "window_seconds": 300,
        "partial": False,
        "source": "live",
    }
    value.update(updates)
    return value


def _bind(run_dir, **updates):
    directory = run_dir / ".state"
    directory.mkdir(parents=True, exist_ok=True)
    binding = {"container_id": CONTAINER, "run_name": run_dir.name}
    binding.update(updates)
    (directory / "caido-runtime.json").write_text(json.dumps(binding))


def _save(run_dir, snapshot=None):
    directory = run_dir / ".state"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "caido-status.json").write_text(json.dumps(snapshot or _sample()))


def _line(seconds, message):
    return f"{(NOW + timedelta(seconds=seconds)).isoformat()} INFO {message}\n"


def test_log_buckets_count_requests_and_unstored_proxy_error_blocks_only():
    collector = _collector()
    log = (
        _line(-301, "startup ready")
        + _line(-300, "http1|logger: GET https://target.invalid/private?token=secret")
        + _line(-299, "http1|logger: GET https://target.invalid/private -> 200")
        + _line(-290, "http2|logger: POST https://target.invalid/submit")
        + _line(-289, "http2|logger: POST https://target.invalid/submit -> 200")
        + _line(-21, "\x1b[31mproxy|logger: Proxying error: Bad Gateway\x1b[0m")
        + "Caused by:\n    0: Failed to acquire connection\n    1: Connection timed out\n"
        + _line(-11, "proxy|logger: Proxying error: Bad Gateway")
        + "Caused by:\n    invalid peer certificate: unknown issuer\n"
        + _line(-1, "proxy|logger: Proxying error: Bad Gateway")
        + "Caused by:\n    Connection refused (os error 111)\n"
        + _line(1, "http1|logger: GET https://future.invalid/")
    )

    result = collector["parse_log"](log, now=NOW, truncated=True)

    assert result["partial"] is False
    assert result["requests_window"] == 2
    assert result["proxy_errors_window"] == 3
    assert result["timeouts_window"] == 1
    assert len(result["series"]) == 30
    assert result["series"][0] == {
        "timestamp": (NOW - timedelta(seconds=300)).isoformat(),
        "requests": 1,
        "errors": 0,
    }
    assert result["series"][1]["requests"] == 1
    assert result["series"][27]["errors"] == 1
    assert result["series"][28]["errors"] == 1
    assert result["series"][29]["errors"] == 1
    assert [item["kind"] for item in result["recent_errors"]] == ["connection", "tls", "timeout"]
    assert "target.invalid" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("text", ["", "unparseable log text", _line(-10, "startup ready")])
def test_incomplete_tail_does_not_invent_window_counts(text):
    result = _collector()["parse_log"](text, now=NOW, truncated=True)
    assert result["partial"] is True
    assert result["requests_window"] is None
    assert result["proxy_errors_window"] is None
    assert result["timeouts_window"] is None
    assert result["series"] == []


def test_partial_window_retains_last_five_observed_errors_without_claiming_complete_counts():
    text = "".join(_line(-index, "proxy|logger: Proxying error: opaque secret") for index in range(8, 0, -1))
    result = _collector()["parse_log"](text, now=NOW, truncated=True)
    assert result["proxy_errors_window"] is None
    assert result["partial"] is True
    assert len(result["recent_errors"]) == 5
    assert all(item["kind"] == "unknown" for item in result["recent_errors"])
    assert result["recent_errors"][0]["timestamp"] == (NOW - timedelta(seconds=1)).isoformat()


def test_missing_log_and_symlink_are_unknown_counts(tmp_path):
    collector = _collector()
    path = tmp_path / "log"
    collector["LOG_PATH"] = str(path)
    assert collector["log_window"](NOW)["requests_window"] is None
    secret = tmp_path / "secret"
    secret.write_text(_line(-1, "http1|logger: GET confidential"))
    path.symlink_to(secret)
    assert collector["log_window"](NOW)["requests_window"] is None


@pytest.mark.parametrize(
    ("checks", "errors", "expected", "reason"),
    [
        ((True, True, 2, NOW.isoformat()), [], "healthy", "traffic_captured"),
        ((True, True, 0, None), [], "ready", "awaiting_traffic"),
        (
            (True, True, 2, NOW.isoformat()),
            [{"timestamp": NOW.isoformat(), "kind": "timeout"}],
            "degraded",
            "recent_proxy_errors",
        ),
        ((False, None, None, None), [], "unavailable", "listener_unreachable"),
        ((True, False, None, None), [], "unknown", "api_unreachable"),
        ((True, True, None, None), [], "unknown", "capture_unknown"),
    ],
)
def test_live_state_uses_probe_and_capture_facts(checks, errors, expected, reason):
    collector = _collector()
    collector["capture_status"] = lambda: checks
    collector["log_window"] = lambda now: {
        "requests_window": None,
        "proxy_errors_window": None,
        "timeouts_window": None,
        "series": [],
        "recent_errors": errors,
        "partial": True,
    }
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        collector["main"]()
    result = json.loads(output.getvalue())
    assert result["state"] == expected
    assert result["reason"] == reason
    assert result["last_activity_at"] == checks[3]
    assert result["partial"] is True


def test_live_collector_calls_only_bound_container_and_persists_whitelisted_snapshot(tmp_path, monkeypatch):
    _bind(tmp_path)
    sample = _sample(raw_traffic="secret-cookie", token="secret-token", url="https://private.invalid/")
    sample["recent_errors"] = [{"timestamp": NOW.isoformat(), "kind": "timeout", "raw": "secret"}]
    container = SimpleNamespace(
        status="running",
        exec_run=Mock(return_value=SimpleNamespace(exit_code=0, output=(json.dumps(sample).encode(), b""))),
    )
    client = SimpleNamespace(containers=SimpleNamespace(get=Mock(return_value=container)), close=Mock())
    monkeypatch.setattr(status.docker, "from_env", Mock(return_value=client))

    result = status.get_proxy_status(tmp_path, live=True, enabled=True)

    client.containers.get.assert_called_once_with(CONTAINER)
    command = container.exec_run.call_args.args[0]
    assert command[:4] == ["timeout", "4", "python3", "-c"]
    assert container.exec_run.call_args.kwargs == {"demux": True}
    assert result["source"] == "live"
    assert result["last_activity_at"] != result["checked_at"]
    saved = (tmp_path / ".state" / "caido-status.json").read_text()
    assert json.loads(saved) == result
    assert "secret" not in saved and "private.invalid" not in saved
    assert (tmp_path / ".state" / "caido-status.json").stat().st_mode & 0o777 == 0o600
    client.close.assert_called_once()


def test_disabled_and_terminal_calls_do_not_access_docker(tmp_path):
    _save(tmp_path)
    disabled = status.get_proxy_status(tmp_path, live=True, enabled=False)
    assert disabled["state"] == "disabled" and disabled["source"] == "none"
    terminal = status.get_proxy_status(tmp_path, live=False, enabled=True)
    assert terminal["state"] == "stopped" and terminal["source"] == "saved"
    assert terminal["checked_at"] == NOW.isoformat()
    assert terminal["metrics"]["captured_total"] == 12
    assert terminal["last_activity_at"] == _sample()["last_activity_at"]


@pytest.mark.parametrize("binding", [None, {"container_id": "short"}, {"run_name": "another-run"}])
def test_missing_or_invalid_binding_cannot_trigger_container_discovery(tmp_path, binding):
    if binding is not None:
        _bind(tmp_path, **binding)
    result = status.get_proxy_status(tmp_path, live=True, enabled=True)
    assert result["state"] == "unknown"
    assert result["reason"] == ("runtime_binding_missing" if binding is None else "runtime_binding_invalid")
    assert result["metrics"]["captured_total"] is None


def test_ttl_reuses_snapshot_then_refreshes_once(tmp_path, monkeypatch):
    _bind(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(status.time, "monotonic", lambda: clock[0])
    collect = Mock(return_value=_sample())
    monkeypatch.setattr(status, "_collect", collect)
    first = status.get_proxy_status(tmp_path, live=True, enabled=True)
    first["metrics"]["captured_total"] = 999
    clock[0] = 9.9
    assert status.get_proxy_status(tmp_path, live=True, enabled=True)["metrics"]["captured_total"] == 12
    collect.assert_called_once()
    clock[0] = 10.0
    status.get_proxy_status(tmp_path, live=True, enabled=True)
    assert collect.call_count == 2


def test_simultaneous_reads_share_one_collection(tmp_path, monkeypatch):
    _bind(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def collect(container_id):
        entered.set()
        assert release.wait(1)
        return _sample()

    mocked = Mock(side_effect=collect)
    monkeypatch.setattr(status, "_collect", mocked)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(status.get_proxy_status, tmp_path, live=True, enabled=True) for _ in range(4)]
        assert entered.wait(1)
        release.set()
        results = [future.result(timeout=2) for future in futures]
    assert mocked.call_count == 1
    assert all(result["metrics"]["captured_total"] == 12 for result in results)


def test_distinct_runs_do_not_share_a_blocking_collection_lock(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    _bind(first)
    _bind(second)
    both_entered = threading.Barrier(2)

    def collect(container_id):
        both_entered.wait(timeout=1)
        return _sample()

    monkeypatch.setattr(status, "_collect", collect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(status.get_proxy_status, path, live=True, enabled=True) for path in (first, second)
        ]
        assert all(call.result(timeout=2)["state"] == "healthy" for call in calls)


@pytest.mark.parametrize(
    ("failure", "state_name", "reason"),
    [
        (RuntimeError("secret-provider-token"), "unknown", "collection_failed"),
        (docker.errors.NotFound("sensitive container detail"), "unavailable", "container_unavailable"),
    ],
)
def test_collection_failure_retains_saved_history_without_refreshing_its_age(
    tmp_path, monkeypatch, failure, state_name, reason
):
    _bind(tmp_path)
    _save(tmp_path)
    monkeypatch.setattr(status, "_collect", Mock(side_effect=failure))
    result = status.get_proxy_status(tmp_path, live=True, enabled=True)
    assert result["state"] == state_name and result["reason"] == reason
    assert result["source"] == "saved" and result["checked_at"] == NOW.isoformat()
    assert result["metrics"]["captured_total"] == 12
    assert "secret" not in json.dumps(result) and "sensitive" not in json.dumps(result)
    assert json.loads((tmp_path / ".state" / "caido-status.json").read_text())["state"] == "healthy"


def test_terminal_without_saved_data_and_corrupt_saved_data_are_unknown_counts(tmp_path):
    result = status.get_proxy_status(tmp_path, live=False, enabled=True)
    assert result["state"] == "stopped" and result["source"] == "none"
    assert result["metrics"]["captured_total"] is None
    _save(tmp_path, {"state": "healthy", "reason": "traffic_captured", "checks": "secret"})
    assert status.get_proxy_status(tmp_path, live=False, enabled=True)["source"] == "none"


@pytest.mark.parametrize("key", ["series", "recent_errors"])
def test_malformed_saved_collections_do_not_fail_terminal_status(tmp_path, key):
    _save(tmp_path, _sample(**{key: {"unexpected": 1}}))
    result = status.get_proxy_status(tmp_path, live=False, enabled=True)
    assert result["state"] == "stopped" and result["source"] == "none"
    assert result["metrics"]["captured_total"] is None


def test_failed_snapshot_write_and_cleanup_do_not_fail_live_status(tmp_path, monkeypatch):
    _bind(tmp_path)
    monkeypatch.setattr(status, "_collect", Mock(return_value=_sample()))
    monkeypatch.setattr(Path, "replace", Mock(side_effect=PermissionError("read-only state")))
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=PermissionError("read-only state")))
    result = status.get_proxy_status(tmp_path, live=True, enabled=True)
    assert result["state"] == "healthy" and result["source"] == "live"


def test_probe_uses_only_local_graphql_and_preserves_real_last_activity(monkeypatch):
    collector = _collector()
    connection = contextlib.nullcontext()
    socket_probe = Mock(return_value=connection)
    monkeypatch.setattr(collector["socket"], "create_connection", socket_probe)
    responses = [
        {"data": {"loginAsGuest": {"token": {"accessToken": "private-token"}}}},
        {
            "data": {
                "requests": {
                    "count": {"value": 18},
                    "edges": [{"node": {"id": "18", "createdAt": "2026-09-05T13:57:00Z"}}],
                }
            }
        },
    ]
    called = []

    def open_local(request, timeout):
        called.append(request)
        assert request.full_url == "http://127.0.0.1:48080/graphql"
        assert timeout == 1.0
        response = SimpleNamespace(read=lambda limit: json.dumps(responses.pop(0)).encode())
        return contextlib.nullcontext(response)

    monkeypatch.setattr(
        collector["urllib"].request, "build_opener", Mock(return_value=SimpleNamespace(open=open_local))
    )
    result = collector["capture_status"]()
    assert result == (True, True, 18, "2026-09-05T13:57:00+00:00")
    socket_probe.assert_called_once_with(("127.0.0.1", 48080), timeout=0.5)
    assert len(called) == 2
    query = json.loads(called[1].data)["query"]
    assert query.startswith("query {") and "count { value }" in query
    assert "createProject" not in query and "raw" not in query and "host" not in query
    assert "private-token" not in json.dumps(result)
