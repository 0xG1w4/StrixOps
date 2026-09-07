"""Bounded, passive Caido telemetry tied to an explicit per-run container binding."""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import docker

CACHE_TTL = 10.0
_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_STATES = {"healthy", "degraded", "ready", "unavailable", "unknown", "stopped", "disabled"}
_REASONS = {
    "proxy_disabled",
    "scan_stopped",
    "runtime_binding_missing",
    "runtime_binding_invalid",
    "container_unavailable",
    "collection_failed",
    "listener_unreachable",
    "api_unreachable",
    "capture_unknown",
    "recent_proxy_errors",
    "traffic_captured",
    "awaiting_traffic",
}
_KINDS = {"timeout", "tls", "connection", "unknown"}
_METRICS = ("captured_total", "requests_window", "proxy_errors_window", "timeouts_window")

# Only aggregates leave the container. No raw log lines, traffic, environment,
# URL, or guest credential is printed or saved. The sole HTTP origin is Caido.
_COLLECTOR = r"""
import json
import os
import re
import socket
import stat
import urllib.request
from datetime import datetime, timedelta, timezone

LOG_PATH = "/tmp/caido_startup.log"
LOG_LIMIT = 262144
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")

def timestamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None

def error_kind(text):
    text = text.lower()
    if re.search(r"timed? out|timeout|deadline", text):
        return "timeout"
    if re.search(r"tls|ssl|certificate|handshake", text):
        return "tls"
    if re.search(r"connect|network|dns|lookup|resolve|no route", text):
        return "connection"
    return "unknown"

def parse_log(text, *, now, truncated=False):
    start = now - timedelta(seconds=300)
    series = [{"timestamp": (start + timedelta(seconds=index * 10)).isoformat(),
               "requests": 0, "errors": 0} for index in range(30)]
    first_stamp = None
    errors = []
    pending = None

    def finish_error():
        if pending is not None and start <= pending[0] <= now:
            errors.append({"timestamp": pending[0].isoformat(), "kind": error_kind(pending[1])})
            index = min(29, int((pending[0] - start).total_seconds() // 10))
            series[index]["errors"] += 1

    for line in ANSI.sub("", text).splitlines():
        match = STAMP.search(line)
        event_time = timestamp(match.group()) if match else None
        if event_time is None:
            if pending is not None:
                pending = (pending[0], pending[1] + "\n" + line)
            continue
        finish_error()
        pending = None
        first_stamp = min(first_stamp, event_time) if first_stamp is not None else event_time
        if "Proxying error" in line:
            pending = (event_time, line)
        if start <= event_time <= now and re.search(r"http[12]\|logger", line) and " -> " not in line:
            index = min(29, int((event_time - start).total_seconds() // 10))
            series[index]["requests"] += 1
    finish_error()
    complete = first_stamp is not None and (not truncated or first_stamp <= start)
    errors.sort(key=lambda item: item["timestamp"], reverse=True)
    return {
        "requests_window": sum(item["requests"] for item in series) if complete else None,
        "proxy_errors_window": len(errors) if complete else None,
        "timeouts_window": sum(item["kind"] == "timeout" for item in errors) if complete else None,
        "series": series if complete else [], "recent_errors": errors[:5], "partial": not complete,
    }

def log_window(now):
    try:
        fd = os.open(LOG_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not regular")
            stream.seek(max(0, info.st_size - LOG_LIMIT))
            raw = stream.read(LOG_LIMIT)
        return parse_log(raw.decode("utf-8", errors="replace"), now=now,
                         truncated=info.st_size > LOG_LIMIT)
    except Exception:
        return parse_log("", now=now)

def capture_status():
    try:
        with socket.create_connection(("127.0.0.1", 48080), timeout=0.5):
            pass
    except OSError:
        return False, None, None, None

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, file, code, message, headers, url):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def query(document, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request("http://127.0.0.1:48080/graphql",
            data=json.dumps({"query": document}).encode(), headers=headers, method="POST")
        with opener.open(request, timeout=1.0) as response:
            raw = response.read(32769)
        if len(raw) > 32768:
            raise ValueError("oversized")
        result = json.loads(raw)
        if result.get("errors") or not isinstance(result.get("data"), dict):
            raise ValueError("invalid graphql")
        return result["data"]

    try:
        login = query("mutation { loginAsGuest { token { accessToken } } }")
        token = (((login.get("loginAsGuest") or {}).get("token") or {}).get("accessToken"))
        if not isinstance(token, str) or not token:
            raise ValueError("login unavailable")
    except Exception:
        return True, False, None, None
    try:
        result = query("query { requests(first: 1, order: {by: CREATED_AT, ordering: DESC}) { "
                       "count { value } edges { node { id createdAt } } } }", token)
        requests = result.get("requests") or {}
        total = (requests.get("count") or {}).get("value")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            total = None
        edges = requests.get("edges") or []
        last = timestamp(edges[0]["node"].get("createdAt")) if edges else None
        return True, True, total, last.isoformat() if last is not None else None
    except Exception:
        return True, True, None, None

def main():
    listener, api, total, last = capture_status()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    window = log_window(now)
    if listener is False:
        state, reason = "unavailable", "listener_unreachable"
    elif api is False:
        state, reason = "unknown", "api_unreachable"
    elif total is None:
        state, reason = "unknown", "capture_unknown"
    elif window["recent_errors"]:
        state, reason = "degraded", "recent_proxy_errors"
    elif total > 0:
        state, reason = "healthy", "traffic_captured"
    else:
        state, reason = "ready", "awaiting_traffic"
    print(json.dumps({
        "state": state, "reason": reason, "checked_at": now.isoformat(), "last_activity_at": last,
        "checks": {"listener": listener, "api": api, "capture": total > 0 if total is not None else None},
        "metrics": {"captured_total": total, "requests_window": window["requests_window"],
                    "proxy_errors_window": window["proxy_errors_window"],
                    "timeouts_window": window["timeouts_window"]},
        "series": window["series"], "recent_errors": window["recent_errors"],
        "window_seconds": 300, "partial": window["partial"] or total is None, "source": "live",
    }), flush=True)

if __name__ == "__main__":
    main()
"""


def _empty(state: str, reason: str) -> dict[str, Any]:
    return {
        "state": state,
        "checked_at": None,
        "last_activity_at": None,
        "reason": reason,
        "checks": {key: None for key in ("listener", "api", "capture")},
        "metrics": {key: None for key in _METRICS},
        "series": [],
        "recent_errors": [],
        "window_seconds": 300,
        "partial": True,
        "source": "none",
    }


def _iso(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC).isoformat() if parsed.tzinfo else None
    except ValueError:
        return None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _clean(value: Any, *, source: str) -> dict[str, Any] | None:
    """Whitelist snapshot fields so neither malformed files nor exec output leak text."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("state"), str)
        or not isinstance(value.get("reason"), str)
        or value["state"] not in _STATES
        or value["reason"] not in _REASONS
    ):
        return None
    if any(not isinstance(value.get(key, {}), dict) for key in ("checks", "metrics")):
        return None
    if any(not isinstance(value.get(key, []), list) for key in ("series", "recent_errors")):
        return None
    result = _empty(value["state"], value["reason"])
    result.update(
        checked_at=_iso(value.get("checked_at")),
        last_activity_at=_iso(value.get("last_activity_at")),
        source=source,
        partial=value.get("partial") is not False,
    )
    for name in result["checks"]:
        item = (value.get("checks") or {}).get(name)
        result["checks"][name] = item if isinstance(item, bool) else None
    for name in _METRICS:
        result["metrics"][name] = _count((value.get("metrics") or {}).get(name))
    for item in (value.get("series") or [])[:30]:
        if isinstance(item, dict) and (stamp := _iso(item.get("timestamp"))) is not None:
            requests, errors = _count(item.get("requests")), _count(item.get("errors"))
            if requests is not None and errors is not None:
                result["series"].append({"timestamp": stamp, "requests": requests, "errors": errors})
    for item in (value.get("recent_errors") or [])[:5]:
        if (
            isinstance(item, dict)
            and (stamp := _iso(item.get("timestamp"))) is not None
            and isinstance(item.get("kind"), str)
            and item["kind"] in _KINDS
        ):
            result["recent_errors"].append({"timestamp": stamp, "kind": item["kind"]})
    return result


def _saved(run_dir: Path, *, state: str, reason: str) -> dict[str, Any]:
    try:
        with (run_dir / ".state" / "caido-status.json").open(encoding="utf-8") as stream:
            raw = stream.read(32769)
        result = _clean(json.loads(raw), source="saved") if len(raw) <= 32768 else None
    except (OSError, ValueError, TypeError, AttributeError):
        result = None
    if result is None:
        return _empty(state, reason)
    result.update(state=state, reason=reason)
    return result


def _save(run_dir: Path, value: dict[str, Any]) -> None:
    directory = run_dir / ".state"
    temporary = directory / f".caido-status-{uuid4().hex}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False)
        temporary.replace(directory / "caido-status.json")
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def _collect(container_id: str) -> dict[str, Any]:
    client = docker.from_env(timeout=6)
    try:
        container = client.containers.get(container_id)
        if container.status != "running":
            raise docker.errors.NotFound("bound container is not running")
        result = container.exec_run(["timeout", "4", "python3", "-c", _COLLECTOR], demux=True)
        if result.exit_code != 0:
            raise ValueError("collector failed")
        stdout, _ = result.output
        if not stdout or len(stdout) > 32768:
            raise ValueError("invalid collector output")
        snapshot = _clean(json.loads(stdout), source="live")
        if snapshot is None or snapshot["checked_at"] is None:
            raise ValueError("invalid collector snapshot")
        return snapshot
    finally:
        client.close()


def get_proxy_status(run_dir: Path, *, live: bool, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return _empty("disabled", "proxy_disabled")
    if not live:
        return _saved(run_dir, state="stopped", reason="scan_stopped")
    binding_path = run_dir / ".state" / "caido-runtime.json"
    try:
        with binding_path.open(encoding="utf-8") as stream:
            raw = stream.read(4097)
        binding = json.loads(raw) if len(raw) <= 4096 else None
    except FileNotFoundError:
        return _saved(run_dir, state="unknown", reason="runtime_binding_missing")
    except (OSError, ValueError):
        return _saved(run_dir, state="unknown", reason="runtime_binding_invalid")
    if (
        not isinstance(binding, dict)
        or binding.get("run_name") != run_dir.name
        or not isinstance(binding.get("container_id"), str)
        or re.fullmatch(r"[0-9a-f]{64}", binding["container_id"]) is None
    ):
        return _saved(run_dir, state="unknown", reason="runtime_binding_invalid")
    key = (str(run_dir.resolve()), binding["container_id"])
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        cached = _CACHE.get(key)
        if cached is not None and time.monotonic() - cached[0] < CACHE_TTL:
            return json.loads(json.dumps(cached[1]))
        try:
            value = _collect(binding["container_id"])
        except docker.errors.NotFound:
            value = _saved(run_dir, state="unavailable", reason="container_unavailable")
        except Exception:
            value = _saved(run_dir, state="unknown", reason="collection_failed")
        else:
            _save(run_dir, value)
        _CACHE[key] = (time.monotonic(), value)
        return json.loads(json.dumps(value))
