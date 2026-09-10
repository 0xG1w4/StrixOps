"""StrixOps console — web API + UI server for the engine.

One process serves the REST API (runs, live event stream, conversation,
findings, reports, hints, scan launch/stop/delete) and, when the static
export exists, the built web UI from ``console/web/out`` mounted at ``/``
after the API routes. Development runs the API and ``next dev`` separately.

Run: ``strixops-console [--runs-root DIR] [--host H] [--port P]``
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import mimetypes
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import weakref
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import MalformedRangeHeader, RangeNotSatisfiable, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from strixops import __version__
from strixops.config.model_options import resolved_api_mode, validate_model_options
from strixops.console import (
    model_catalog,
    model_probe,
    parser,
    project_assignment,
    project_reports,
    project_scope,
    project_skills,
    projects_store,
    prompt_probe,
    proxy_status,
    settings_store,
)
from strixops.engine.targets import MAX_TARGETS, normalize_targets
from strixops.platform.runname import generate_run_name

RUN_SUFFIX_LENGTH = 4  # `<slug>_<4hex>`
LIVE_GRACE_SECONDS = 300  # events.jsonl quiet longer than this → stale
ENGINE_LOG_TAIL_LINES = 200
STREAM_POLL_SECONDS = 1.0
STREAM_DEADLINE_SECONDS = 6 * 3600
MODEL_PROBE_DISCONNECT_POLL_SECONDS = 0.2
REPORT_FILENAME = "penetration_test_report.md"
LAUNCH_SIDECAR = ".console_launch.json"  # launch facts the engine does not record


def _runs_root_from(env_override: str = "") -> Path:
    raw = (env_override or os.environ.get("STRIX_RUNS") or "").strip()
    return Path(raw).expanduser() if raw else Path.cwd() / "strix_runs"


class ConsoleState:
    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root
        # scan_id -> {pid, run_name, target, popen}
        self.scans: dict[str, dict[str, Any]] = {}
        # run_name -> (cache_key, summary-without-dynamic-fields)
        self.summary_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}

    def run_dir(self, name: str) -> Path:
        if name in {"", ".", ".."}:
            raise HTTPException(status_code=400, detail="invalid run name")
        path = self.runs_root / name
        if not path.is_dir():
            raise HTTPException(status_code=404, detail=f"unknown run {name!r}")
        return path


state: ConsoleState = ConsoleState(_runs_root_from())

app = FastAPI(title="StrixOps Console", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(prompt_probe.router)

# Independent traffic tasks share only the Console HTTP surface, not scan execution.
from strixops.traffic.mcp_server import install as install_mcp_tasks  # noqa: E402

install_mcp_tasks(app)


# ------------------------------------------------------------------ liveness


def _events_mtime(run_dir: Path) -> float:
    """mtime of events.jsonl — the ONLY liveness heartbeat (0.0 when absent).

    No directory-mtime fallback: touching the run dir (hints, snapshots) must
    never resurrect a dead run as "live".
    """
    try:
        return (run_dir / "events.jsonl").stat().st_mtime
    except OSError:
        return 0.0


def _pid_for(run_name: str, run_dir: Path) -> int | None:
    """Engine pid for a run: console-tracked first, then ``<run_dir>/.console.pid``."""
    for info in state.scans.values():
        if info.get("run_name") == run_name:
            pid = info.get("pid")
            return int(pid) if pid else None
    try:
        return int((run_dir / ".console.pid").read_text(encoding="utf-8").strip() or 0) or None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False
    return True


def _engine_exit_code(run_name: str) -> int | None:
    """Tracked engine's exit code, reaped via Popen.poll(); None while running."""
    for info in state.scans.values():
        if info.get("run_name") != run_name:
            continue
        popen = info.get("popen")
        return popen.poll() if popen is not None else None
    return None


# --------------------------------------------------------------------- runs


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _summary_key(run_dir: Path) -> tuple[Any, ...]:
    """Cache key covering every file the static summary fields derive from."""
    parts: list[Any] = [
        _mtime_ns(run_dir / rel)
        for rel in (
            "run.json",
            "events.jsonl",
            "vulnerabilities.json",
            project_assignment.ASSIGNMENT_FILE,
            LAUNCH_SIDECAR,
        )
    ]
    internal = run_dir / "internal_findings"
    if internal.is_dir():
        parts.append(tuple(sorted((p.name, _mtime_ns(p)) for p in internal.glob("*.md"))))
    else:
        parts.append(None)
    return tuple(parts)


def _run_project_id(run_dir: Path) -> str:
    """Resolve console-owned membership without trusting engine rewrites."""
    try:
        return project_assignment.project_id_for_run(run_dir)
    except project_assignment.AssignmentStoreError:
        # Malformed explicit metadata fails closed as unassigned.  Assignment
        # endpoints surface persistence errors when the operator next edits it.
        return ""


def _build_summary(run_dir: Path) -> dict[str, Any]:
    record = parser.json_load(run_dir / "run.json")
    scan_config = record.get("scan_config") or {}
    launch_meta = parser.json_load(run_dir / LAUNCH_SIDECAR)
    target_config = scan_config if "target" in scan_config or "targets" in scan_config else launch_meta
    try:
        targets = parser.scan_targets(target_config)
        target_error = ""
    except ValueError as exc:
        targets = []
        target_error = str(exc)
    findings = parser.parse_findings(run_dir)
    severity_counts: dict[str, int] = {}
    for vuln in findings["vulnerabilities"]:
        sev = str(vuln.get("severity") or "info").lower()
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    summary: dict[str, Any] = {
        "name": run_dir.name,
        "target": targets[0] if targets else scan_config.get("target") or launch_meta.get("target") or "",
        "targets": targets,
        "target_count": len(targets),
        **({"target_error": target_error} if target_error else {}),
        "scan_type": scan_config.get("scan_type") or launch_meta.get("scan_type") or "web",
        "engine": str(scan_config.get("engine") or launch_meta.get("engine") or "ops"),
        "model_transport": str(
            scan_config.get("model_transport") or launch_meta.get("model_transport") or ""
        ),
        "project_id": _run_project_id(run_dir),
        "status": record.get("status") or "unknown",
        "start_time": record.get("start_time") or "",
        "end_time": record.get("end_time") or "",
        "duration_seconds": record.get("duration_seconds"),
        "vulnerability_count": len(findings["vulnerabilities"]),
        "internal_finding_count": len(findings["internal"]),
        "severity": severity_counts,
        "mtime": _events_mtime(run_dir),
        # Launch-info surface: proxy/transport, crypto focus, model route and
        # dry-run flag. Values come from the engine's run.json scan_config
        # where it records them, with the console's launch sidecar filling
        # the gap for runs launched before the engine recorded them.
        "socks5": str(scan_config.get("socks5_proxy") or ""),
        "gsocket": str(scan_config.get("gsocket_key") or ""),
        "crypto": bool(scan_config.get("crypto_mode")),
        "model": str(scan_config.get("model") or launch_meta.get("model") or ""),
        "llm_api_mode": str(scan_config.get("llm_api_mode") or launch_meta.get("llm_api_mode") or ""),
        "llm_reasoning_effort": str(
            scan_config.get("llm_reasoning_effort") or launch_meta.get("llm_reasoning_effort") or ""
        ),
    }
    dry_run = scan_config.get("dry_run", launch_meta.get("dry_run"))
    if dry_run is not None:
        summary["dry_run"] = bool(dry_run)
    if record.get("failure_reason"):
        summary["failure_reason"] = str(record["failure_reason"])
    llm_usage = record.get("llm_usage")
    if isinstance(llm_usage, dict):
        summary["llm_usage"] = llm_usage
    for key in ("evidence", "workspace", "sandbox_runtime"):
        if isinstance(record.get(key), dict):
            summary[key] = record[key]
    return summary


def _run_summary(run_dir: Path, *, cache: bool = True) -> dict[str, Any]:
    """RunSummary — static parts cached by file mtimes, liveness computed fresh."""
    key = _summary_key(run_dir)
    cached = state.summary_cache.get(run_dir.name)
    if cache and cached is not None and cached[0] == key:
        base = cached[1]
    else:
        base = _build_summary(run_dir)
        state.summary_cache[run_dir.name] = (key, base)
    summary = dict(base)
    status = summary["status"]
    fresh = summary["mtime"] > 0 and (time.time() - summary["mtime"]) < LIVE_GRACE_SECONDS
    pid_alive = _pid_alive(_pid_for(summary["name"], run_dir))
    summary["live"] = status == "running" and (fresh or pid_alive)
    summary["stale"] = status == "running" and not fresh
    exit_code = _engine_exit_code(summary["name"])
    if exit_code is not None:
        summary["engine_exit_code"] = exit_code
    return summary


def _assert_run_scope(scope: dict[str, Any], summary: dict[str, Any]) -> None:
    """Every recorded asset must fit the project, including secondary targets."""
    if summary.get("target_error"):
        raise ValueError("Run target metadata is invalid; assignment cannot be verified.")
    targets = parser.scan_targets(summary)
    if not targets:
        raise ValueError("Run has no target metadata; assignment cannot be verified.")
    for target in targets:
        project_scope.assert_target_allowed(scope, target, summary.get("scan_type") or "web")


def _iter_run_dirs() -> list[Path]:
    """Directories under runs_root that look like engine runs."""
    if not state.runs_root.is_dir():
        return []
    dirs = []
    for path in state.runs_root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        if not (path / "run.json").exists() and not (path / "events.jsonl").exists():
            continue
        dirs.append(path)
    return dirs


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "product": "StrixOps",
        "version": __version__,
        "runs_root": str(state.runs_root),
        "live_runs": _live_run_count(),
    }


def _live_run_count() -> int:
    return int(sum(1 for run_dir in _iter_run_dirs() if _run_summary(run_dir)["live"]))


@app.get("/api/runs")
def list_runs() -> dict:
    items = [_run_summary(run_dir) for run_dir in _iter_run_dirs()]
    items.sort(key=lambda item: item.get("mtime") or 0, reverse=True)
    totals: dict[str, int] = {}
    for item in items:
        for sev, count in item["severity"].items():
            totals[sev] = totals.get(sev, 0) + count
    live = sum(1 for item in items if item["live"])
    return {"runs": items, "totals": {"severity": totals, "runs": len(items), "live": live}}


@app.get("/api/runs/{name}")
def run_detail(name: str) -> dict:
    run_dir = state.run_dir(name)
    summary = _run_summary(run_dir)
    summary["agents"] = parser.json_load(run_dir / ".state" / "agents.json")
    summary["hints_summary"] = _hints_summary(run_dir)
    return summary


@app.get("/api/runs/{name}/proxy")
def run_proxy_status(name: str) -> dict:
    run_dir = state.run_dir(name)
    summary = _run_summary(run_dir)
    return proxy_status.get_proxy_status(
        run_dir,
        live=summary["live"],
        enabled=summary.get("scan_type") == "web" and not summary.get("dry_run", False),
    )


@app.get("/api/runs/{name}/events")
def run_events(name: str, after: int = -1, limit: int = 500) -> dict:
    run_dir = state.run_dir(name)
    pairs = parser.read_event_lines(run_dir / "events.jsonl")
    events: list[dict[str, Any]] = []
    cursor = after
    for index, line in pairs:
        if index <= after:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            cursor = index  # consumed even though unreturnable
            continue
        if len(events) >= limit:
            break  # stop BEFORE advancing the cursor past unreturned events
        events.append(event)
        cursor = index
    return {
        "events": events,
        "cursor": cursor,
        "total": len(pairs),
        "live": _run_summary(run_dir)["live"],
    }


@app.get("/api/runs/{name}/conversation")
def run_conversation(name: str, after: int = -1) -> dict:
    run_dir = state.run_dir(name)
    messages, cursor = parser.read_conversation(run_dir / "events.jsonl", after=after)
    return {"messages": messages, "cursor": cursor, "live": _run_summary(run_dir)["live"]}


@app.get("/api/runs/{name}/findings")
def run_findings(name: str) -> dict:
    return parser.parse_findings(state.run_dir(name))


def _valid_assessment_document(data: Any) -> bool:
    """Validate the render boundary; corrupt records remain unknown, not clean."""
    from strixops.report.assessment import OUTCOMES

    def strings(item: Any, required: tuple = (), optional: tuple = ()) -> bool:
        return (
            isinstance(item, dict)
            and all(isinstance(item.get(key), str) for key in required)
            and all(item.get(key) is None or isinstance(item[key], str) for key in optional)
        )

    def history(item: dict) -> bool:
        value = item.get("history")
        return value is None or (isinstance(value, list) and all(isinstance(row, dict) for row in value))

    if not (
        isinstance(data, dict)
        and type(data.get("schema_version")) is int
        and data["schema_version"] == 1
        and strings(data, optional=("generated_at", "interpretation"))
    ):
        return False
    if not strings(data.get("lifecycle"), required=("status",)):
        return False
    coverage, models = data.get("coverage"), data.get("threat_models")
    if not (
        strings(coverage, required=("status",))
        and strings(models, required=("status",))
        and coverage.get("status") in {"unknown", "recorded"}
        and models.get("status") in {"unknown", "recorded"}
        and isinstance(coverage.get("entries"), list)
        and isinstance(models.get("models"), list)
    ):
        return False
    for entry in coverage["entries"]:
        if not (
            strings(
                entry,
                required=("surface", "risk_area", "outcome"),
                optional=(
                    "id",
                    "entry_id",
                    "evidence",
                    "agent_id",
                    "agent_name",
                    "timestamp",
                    "created_by",
                    "created_by_name",
                    "created_at",
                    "updated_at",
                ),
            )
            and entry["outcome"] in OUTCOMES
            and history(entry)
        ):
            return False
    count = coverage.get("unresolved_count")
    if coverage["status"] == "unknown":
        if coverage["entries"] or count is not None:
            return False
    elif (
        not coverage["entries"]
        or type(count) is not int
        or count != sum(entry["outcome"] == "needs_follow_up" for entry in coverage["entries"])
    ):
        return False
    if models["status"] == "unknown" and models["models"]:
        return False
    for model in models["models"]:
        if not (
            strings(
                model,
                required=("target", "content"),
                optional=("written_by", "written_by_name", "updated_at"),
            )
            and history(model)
        ):
            return False
        revision = model.get("revision")
        if revision is not None and (type(revision) is not int or revision < 1):
            return False
        amendments = model.get("amendments")
        if amendments is not None and not (
            isinstance(amendments, list)
            and all(
                strings(item, required=("content",), optional=("agent_id", "agent_name", "timestamp"))
                for item in amendments
            )
        ):
            return False
    return True


@app.get("/api/runs/{name}/assessment")
def run_assessment(name: str) -> dict:
    """Read the run-owned assessment; missing history never means zero gaps."""
    from strixops.report.assessment import empty_assessment

    run_dir = state.run_dir(name)
    status = str(parser.json_load(run_dir / "run.json").get("status") or "unknown")
    source_status = "missing"
    try:
        with os.fdopen(_open_run_file(run_dir, "assessment.json"), "rb") as source:
            raw = source.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("assessment exceeds the read limit")

        def invalid_constant(_value: str) -> None:
            raise ValueError("invalid JSON constant")

        data = json.loads(
            raw.decode("utf-8"), parse_constant=invalid_constant, parse_float=parser.finite_json_float
        )
        if not _valid_assessment_document(data):
            raise ValueError("unsupported assessment")
        return {**data, "source_status": "available"}
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError, RecursionError):
        source_status = "unreadable"
    return {**empty_assessment(status), "source_status": source_status}


@app.get("/api/runs/{name}/report")
def run_report(name: str) -> dict:
    path = state.run_dir(name) / REPORT_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="no report yet")
    return {"markdown": path.read_text(encoding="utf-8", errors="replace")}


@app.get("/api/runs/{name}/log")
def run_log(name: str) -> dict:
    path = state.run_dir(name) / "engine.log"
    if not path.is_file():
        return {"text": ""}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"text": ""}
    return {"text": "\n".join(lines[-ENGINE_LOG_TAIL_LINES:])}


def _relative_file_parts(relative: str) -> tuple[str, ...]:
    path = Path(relative)
    if not relative or path.is_absolute() or not path.parts or ".." in path.parts or "\x00" in relative:
        raise ValueError("invalid artifact path")
    return path.parts


def _open_run_file(run_dir: Path, relative: str) -> int:
    """Open a regular file beneath an anchored directory, never following links."""
    parts = _relative_file_parts(relative)
    directory = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError("artifact is not a regular file")
        return fd
    finally:
        os.close(directory)


def _artifact_visible(relative: str, evidence_names: set[str]) -> bool:
    parts = _relative_file_parts(relative)
    if parts[0] == "evidence":
        return Path(*parts[1:]).as_posix() in evidence_names
    return not any(part.startswith(".") for part in parts)


def _artifact_files(run_dir: Path) -> list[dict[str, Any]]:
    """Visible regular files; hidden evidence is included only through its manifest."""
    evidence_names = {entry["filename"] for entry in _evidence_entries(run_dir) if entry["deliverable"]}
    files: list[dict[str, Any]] = []
    try:
        base_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return files
    try:
        for root, dirs, names, directory in os.fwalk(".", dir_fd=base_fd, follow_symlinks=False):
            for name in names:
                relative = (Path(root) / name).as_posix()
                if not _artifact_visible(relative, evidence_names):
                    continue
                try:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    files.append({"path": relative, "size": info.st_size, "mtime": info.st_mtime})
            # Hidden directories only need traversal beneath the evidence archive.
            if Path(root).parts[:1] != ("evidence",):
                dirs[:] = [name for name in dirs if not name.startswith(".")]
    finally:
        os.close(base_fd)
    # Shallow-first so the tree lists root artifacts before nested dirs.
    files.sort(key=lambda item: (item["path"].count("/"), item["path"].lower()))
    return files


@app.get("/api/runs/{name}/artifacts")
def run_artifacts_index(name: str) -> dict:
    return {"files": _artifact_files(state.run_dir(name))}


@app.get("/api/runs/{name}/artifacts/{path:path}")
def run_artifact(name: str, path: str, request: Request) -> Response:
    run_dir = state.run_dir(name)
    try:
        evidence_names = {entry["filename"] for entry in _evidence_entries(run_dir) if entry["deliverable"]}
        if not _artifact_visible(path, evidence_names):
            raise ValueError("hidden artifact")
        fd = _open_run_file(run_dir, path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    return _file_stream(fd, mimetypes.guess_type(path)[0] or "application/octet-stream", request)


class _OwnedFileStream(StreamingResponse):
    """Close the descriptor even when response startup or sending is interrupted."""

    def __init__(self, source, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._close_file = weakref.finalize(self, source.close)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._close_file()


def _file_stream(fd: int, media_type: str, request: Request | None = None) -> Response:
    """Retain FileResponse's headers/ranges while reading only the owned descriptor."""
    source = os.fdopen(fd, "rb")
    try:
        info = os.fstat(source.fileno())
        # Reuse Starlette's current header/range semantics, but never call this
        # FileResponse: its path-opening and pathsend branches are not safe here.
        metadata = FileResponse("", media_type=media_type, stat_result=info)
        headers = dict(metadata.headers)
        request_headers = request.headers if request is not None else {}
        if_none_match = request_headers.get("if-none-match")
        if if_none_match and any(
            tag.strip() == "*" or tag.strip().removeprefix("W/") == headers["etag"]
            for tag in if_none_match.split(",")
        ):
            source.close()
            headers.pop("content-length", None)
            return Response(status_code=304, headers=headers)

        status_code = 200
        ranges = [(0, info.st_size)]
        range_header = request_headers.get("range")
        if_range = request_headers.get("if-range")
        if range_header and (if_range is None or metadata._should_use_range(if_range)):
            try:
                requested_ranges = metadata._parse_range_header(range_header, info.st_size)
            except MalformedRangeHeader as exc:
                source.close()
                return Response(exc.content, status_code=400, media_type="text/plain")
            except RangeNotSatisfiable:
                source.close()
                headers.update({"content-range": f"bytes */{info.st_size}", "content-length": "0"})
                return Response(status_code=416, headers=headers)
            if requested_ranges:
                ranges = requested_ranges
                status_code = 206
                if len(ranges) == 1:
                    start, end = ranges[0]
                    headers["content-range"] = f"bytes {start}-{end - 1}/{info.st_size}"
                    headers["content-length"] = str(end - start)

        boundary = ""
        header_generator = None
        if len(ranges) > 1:
            boundary = secrets.token_hex(13)
            length, header_generator = metadata.generate_multipart(
                ranges, boundary, info.st_size, headers["content-type"]
            )
            headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
            headers["content-length"] = str(length)

        def chunks():
            try:
                if request is not None and request.method == "HEAD":
                    return
                for start, end in ranges:
                    if header_generator is not None:
                        yield header_generator(start, end)
                    source.seek(start)
                    remaining = end - start
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk
                    if header_generator is not None:
                        yield b"\r\n"
                if header_generator is not None:
                    yield f"--{boundary}--".encode("latin-1")
            finally:
                source.close()

        return _OwnedFileStream(source, chunks(), status_code=status_code, headers=headers)
    except BaseException:
        source.close()
        raise


def _unlink_quiet(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


@app.get("/api/runs/{name}/archive")
def run_archive(name: str) -> FileResponse:
    """Zip visible artifacts using anchored descriptors, including delivered evidence."""
    run_dir = state.run_dir(name)
    fd, tmp_name = tempfile.mkstemp(suffix=".zip", prefix="strixops-archive-")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as bundle:
            for item in _artifact_files(run_dir):
                fd = _open_run_file(run_dir, item["path"])
                with (
                    os.fdopen(fd, "rb") as source,
                    bundle.open(f"{run_dir.name}/{item['path']}", "w", force_zip64=True) as output,
                ):
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except (OSError, ValueError) as exc:
        _unlink_quiet(tmp_name)
        raise HTTPException(status_code=500, detail=f"archive failed: {exc}") from exc
    return FileResponse(
        tmp_name,
        media_type="application/zip",
        filename=f"{run_dir.name}.zip",
        background=BackgroundTask(_unlink_quiet, tmp_name),
    )


# ------------------------------------------------------------------- stream


def _stream_close_status(run_dir: Path, summary: dict[str, Any]) -> str | None:
    """Status to close the SSE stream with, or None to keep streaming."""
    status = summary["status"]
    if status not in ("running", "unknown", ""):
        return str(status)
    if status == "running":
        return None if summary["live"] else "stale"
    # Unknown status: the engine never wrote run.json. Keep the stream open
    # only while a tracked engine process is alive or events exist (an
    # externally started run mid-startup); otherwise the launch died.
    if _pid_alive(_pid_for(summary["name"], run_dir)):
        return None
    try:
        return None if (run_dir / "events.jsonl").stat().st_size > 0 else "unknown"
    except OSError:
        return "unknown"


def _sse_run_closed(status: str) -> str:
    return f"event: run_closed\ndata: {json.dumps({'status': status})}\n\n"


@app.get("/api/runs/{name}/stream")
async def run_stream(name: str, request: Request, after: int | None = None) -> StreamingResponse:
    run_dir = state.run_dir(name)
    # Reconnect resume: Last-Event-ID (sent by EventSource on auto-reconnect)
    # wins over the explicit ?after= cursor, which wins over a full replay.
    cursor: int | None = None
    last_event_id = request.headers.get("last-event-id", "")
    if last_event_id:
        with contextlib.suppress(ValueError):
            cursor = int(last_event_id)
    if cursor is None:
        cursor = after if after is not None else -1

    async def generator() -> AsyncIterator[str]:
        sent = cursor
        events_file = run_dir / "events.jsonl"
        deadline = time.monotonic() + STREAM_DEADLINE_SECONDS
        while True:
            for index, line in parser.read_event_lines(events_file):
                if index <= sent:
                    continue
                sent = index
                yield f"id: {index}\ndata: {line}\n\n"
            if time.monotonic() >= deadline:
                yield _sse_run_closed("deadline")
                return
            close_status = _stream_close_status(run_dir, _run_summary(run_dir))
            if close_status is not None:
                yield _sse_run_closed(close_status)
                return
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# -------------------------------------------------------------------- hints


class HintBody(BaseModel):
    message: str
    agent_id: str = ""
    phase: str = "1"


def _load_hints(run_dir: Path) -> list[dict[str, Any]]:
    hints_dir = run_dir / "operator_hints"
    if not hints_dir.is_dir():
        return []
    acked = parser.hint_ack_tokens(run_dir / "events.jsonl")
    hints: list[dict[str, Any]] = []
    for path in hints_dir.glob("*.json"):
        hint = parser.json_load(path)
        if not hint:
            continue
        status = str(hint.get("status") or "queued")
        token = str(hint.get("hint_token") or "")
        if token and token in acked:
            status = "acked"
        hints.append(
            {
                "message_id": str(hint.get("message_id") or path.stem),
                "agent_id": str(hint.get("agent_id") or ""),
                "agent_name": str(hint.get("agent_name") or ""),
                "message": str(hint.get("message") or ""),
                "status": status,
                "hint_token": token,
                "created_at": str(hint.get("created_at") or ""),
            }
        )
    hints.sort(key=lambda hint: (hint["created_at"], hint["message_id"]))
    return hints


def _hints_summary(run_dir: Path) -> dict[str, int]:
    hints = _load_hints(run_dir)
    delivered = sum(1 for hint in hints if hint["status"] in ("delivered", "acked"))
    return {"total": len(hints), "delivered": delivered}


@app.get("/api/runs/{name}/hints")
def get_hints(name: str) -> dict:
    return {"hints": _load_hints(state.run_dir(name))}


@app.post("/api/runs/{name}/hints")
async def post_hint(name: str, body: HintBody) -> dict:
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    run_dir = state.run_dir(name)
    hints_dir = run_dir / "operator_hints"
    hints_dir.mkdir(parents=True, exist_ok=True)
    message_id = uuid.uuid4().hex[:12]
    hint = {
        "message_id": message_id,
        "task_id": "",
        "phase": body.phase or "1",
        "target": "",
        "agent_id": body.agent_id,
        "agent_name": "",
        "message": body.message,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "queued",
        "hint_token": secrets.token_hex(4),
    }
    payload = json.dumps(hint, ensure_ascii=False)
    tmp = hints_dir / f".{int(time.time())}_{message_id}.json.tmp"
    final = hints_dir / f"{int(time.time())}_{message_id}.json"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(final)
    return {"ok": True, "message_id": message_id, "hint_token": hint["hint_token"]}


# ------------------------------------------------------------ stop / delete


@app.post("/api/runs/{name}/stop")
def stop_run(name: str) -> dict:
    run_dir = state.run_dir(name)
    pid = _pid_for(name, run_dir)
    if not pid:
        return {"ok": False, "detail": "no engine pid recorded for this run"}
    if not _pid_alive(pid):
        return {"ok": True, "detail": "engine already exited"}
    try:
        # Launch uses start_new_session=True, so the engine pid is the pgid;
        # signaling the group also reaches engine-spawned children.
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            return {"ok": False, "detail": f"failed to signal engine: {exc}"}
    return {"ok": True}


@app.delete("/api/runs/{name}")
def delete_run(name: str) -> dict:
    run_dir = state.run_dir(name)
    resolved = run_dir.resolve()
    if not resolved.is_relative_to(state.runs_root.resolve()):
        raise HTTPException(status_code=400, detail="path escapes runs root")
    if _run_summary(run_dir)["live"]:
        raise HTTPException(status_code=409, detail="run is live — stop it first")
    try:
        shutil.rmtree(resolved)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"delete failed: {exc}") from exc
    state.summary_cache.pop(run_dir.name, None)
    return {"ok": True}


# ------------------------------------------------------------------ settings


class ProfileBody(BaseModel):
    name: str = ""
    route_type: str = "custom"
    llm_api_base: str = ""
    llm_api_key: str = ""
    model_web: str = ""
    model_internal: str = ""
    api_mode_web: str = "chat_completions"
    api_mode_internal: str = "chat_completions"
    reasoning_effort_web: str = "default"
    reasoning_effort_internal: str = "default"
    copy_from_profile_id: str | None = None


class ModelCatalogBody(BaseModel):
    profile_id: str | None = None
    route_type: str = "custom"
    llm_api_base: str = ""
    llm_api_key: str = ""


class ModelProbeBody(ModelCatalogBody):
    model: str = ""
    api_mode: str = "auto"
    reasoning_effort: str = "default"


@app.post("/api/settings/test-model")
async def test_provider_model(body: ModelProbeBody, request: Request) -> dict:
    probe = asyncio.create_task(model_probe.test_model(**body.model_dump()))
    try:
        while not probe.done():
            done, _ = await asyncio.wait({probe}, timeout=MODEL_PROBE_DISCONNECT_POLL_SECONDS)
            if done:
                break
            # FastAPI has already parsed the request body, so checking for
            # disconnect here cannot compete with a body reader.
            if await request.is_disconnected():
                raise HTTPException(status_code=499, detail="model test canceled")
        return await probe
    finally:
        if not probe.done():
            probe.cancel()
        # Await cancellation so the probe closes its streaming HTTP client
        # before this request ends, including cancellation of the handler.
        await asyncio.gather(probe, return_exceptions=True)


@app.post("/api/settings/models")
def list_provider_models(body: ModelCatalogBody) -> dict:
    return model_catalog.discover_models(**body.model_dump())


@app.get("/api/settings")
def get_settings() -> dict:
    data = settings_store.load_settings()
    return {
        "profiles": [settings_store.public_profile(p) for p in data["profiles"]],
        "active_profile_id": data["active_profile_id"],
    }


def _find_profile(data: dict, profile_id: str) -> dict | None:
    for profile in data["profiles"]:
        if profile.get("id") == profile_id:
            return profile
    return None


@app.post("/api/settings/profiles")
def create_profile(body: ProfileBody) -> dict:
    data = settings_store.load_settings()
    copy_source = None
    if body.copy_from_profile_id:
        copy_source = _find_profile(data, body.copy_from_profile_id)
        if copy_source is None:
            raise HTTPException(status_code=404, detail="unknown source profile")
    profile, errors = settings_store.sanitize_profile(
        body.model_dump(exclude_unset=True), copy_source=copy_source
    )
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    data["profiles"].append(profile)
    if data["active_profile_id"] is None:
        data["active_profile_id"] = profile["id"]  # first profile auto-activates
    settings_store.save_settings(data)
    return {"ok": True, "profile": settings_store.public_profile(profile)}


@app.patch("/api/settings/profiles/{profile_id}")
def update_profile(profile_id: str, body: ProfileBody) -> dict:
    data = settings_store.load_settings()
    existing = _find_profile(data, profile_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="unknown profile")
    if body.copy_from_profile_id:
        raise HTTPException(status_code=400, detail="copy_from_profile_id is only valid for a new profile")
    profile, errors = settings_store.sanitize_profile(body.model_dump(exclude_unset=True), existing=existing)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    data["profiles"] = [profile if p.get("id") == profile_id else p for p in data["profiles"]]
    settings_store.save_settings(data)
    return {"ok": True, "profile": settings_store.public_profile(profile)}


@app.delete("/api/settings/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict:
    data = settings_store.load_settings()
    if _find_profile(data, profile_id) is None:
        raise HTTPException(status_code=404, detail="unknown profile")
    data["profiles"] = [p for p in data["profiles"] if p.get("id") != profile_id]
    if data["active_profile_id"] == profile_id:
        data["active_profile_id"] = data["profiles"][0]["id"] if data["profiles"] else None
    settings_store.save_settings(data)
    return {"ok": True}


@app.post("/api/settings/activate")
def activate_profile(body: dict) -> dict:
    profile_id = str((body or {}).get("profile_id") or "")
    data = settings_store.load_settings()
    if _find_profile(data, profile_id) is None:
        raise HTTPException(status_code=404, detail="unknown profile")
    data["active_profile_id"] = profile_id
    settings_store.save_settings(data)
    return {"ok": True, "active_profile_id": profile_id}


# --------------------------------------------------------------- integrations


class IntegrationBody(BaseModel):
    perplexity_api_key: str = ""


@app.get("/api/settings/integrations")
def get_integrations() -> dict:
    data = settings_store.load_settings()
    key = str(data.get("integrations", {}).get("perplexity_api_key") or "")
    return {
        "perplexity_api_key_set": bool(key),
        "perplexity_api_key_masked": settings_store.mask_key(key) if key else "",
    }


@app.put("/api/settings/integrations")
def set_integrations(body: IntegrationBody) -> dict:
    data = settings_store.load_settings()
    new_key = body.perplexity_api_key.strip()
    # masked value or empty = keep current
    current = str(data.get("integrations", {}).get("perplexity_api_key") or "")
    if settings_store.is_masked(new_key) or (not new_key and current):
        new_key = current
    data.setdefault("integrations", {})
    data["integrations"]["perplexity_api_key"] = new_key
    settings_store.save_settings(data)
    return {
        "ok": True,
        "perplexity_api_key_set": bool(new_key),
        "perplexity_api_key_masked": settings_store.mask_key(new_key) if new_key else "",
    }


# -------------------------------------------------------------------- launch


class ScanBody(BaseModel):
    target: str = ""
    targets: list[str] | None = Field(default=None, min_length=1, max_length=MAX_TARGETS)
    scan_type: str = "web"
    crypto: bool = False
    socks5: str = ""
    gsocket: str = ""
    instruction: str = ""
    dry_run: bool = False
    profile_id: str = ""
    language: str = "zh-CN"
    project_id: str = ""
    llm_api_base: str = ""
    llm_api_key: str = ""
    strix_llm: str = ""
    llm_api_mode: str = "chat_completions"
    llm_reasoning_effort: str = "default"

    @model_validator(mode="before")
    @classmethod
    def formal_execution_only(cls, value: Any) -> Any:
        if isinstance(value, dict) and (
            value.get("engine", "ops") != "ops" or any(str(key).startswith("prototype_") for key in value)
        ):
            raise ValueError("This release uses the integrated StrixOps scanner only.")
        return value


def _resolve_llm_env(body: ScanBody) -> dict[str, str]:
    """LLM env for a live launch: explicit profile > inline fields > active.

    Profiles live server-side (~/.strixops/console.json) and their keys never
    round-trip through the browser.
    """
    data = settings_store.load_settings()
    profile = None
    if body.profile_id:
        profile = _find_profile(data, body.profile_id)
        if profile is None:
            raise HTTPException(status_code=400, detail=f"unknown profile {body.profile_id!r}")
    elif not (body.llm_api_base and body.llm_api_key and body.strix_llm):
        active_id = data["active_profile_id"]
        profile = _find_profile(data, active_id) if active_id else None
    if profile is not None:
        return settings_store.effective_llm(profile, body.scan_type)
    if body.llm_api_base and body.llm_api_key and body.strix_llm:
        return {
            "llm_api_base": body.llm_api_base,
            "llm_api_key": body.llm_api_key,
            "strix_llm": body.strix_llm,
            "llm_api_mode": body.llm_api_mode,
            "llm_reasoning_effort": body.llm_reasoning_effort,
        }
    raise HTTPException(
        status_code=400,
        detail="no model profile — create one in Settings (or pass llm fields inline)",
    )


def _requested_targets(body: ScanBody) -> list[str]:
    try:
        return normalize_targets(body.target, body.targets)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validated_launch_project(body: ScanBody) -> dict[str, Any] | None:
    """Validate every target before creating a run or starting any process."""
    targets = _requested_targets(body)
    for target in targets:
        try:
            project_scope.normalize_target(target, body.scan_type)
        except project_scope.TargetValidationError as exc:
            raise HTTPException(status_code=400, detail=f"{target}: {exc}") from exc
    if not body.project_id:
        return None
    data = projects_store.load_projects()
    project = projects_store.find_project(data, body.project_id)
    if project is None:
        raise HTTPException(status_code=400, detail=f"unknown project {body.project_id!r}")
    try:
        scope = projects_store.scope_for_project(project)
        for target in targets:
            project_scope.assert_target_allowed(scope, target, body.scan_type)
    except (project_scope.ScopeValidationError, project_scope.TargetValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except project_scope.TargetOutOfScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return project


class TargetsPreflightBody(BaseModel):
    targets: list[str] = Field(min_length=1, max_length=MAX_TARGETS)
    scan_type: Literal["web", "internal"] = "web"
    project_id: str = ""


@app.post("/api/scans/validate-targets")
def validate_scan_targets(body: TargetsPreflightBody) -> dict:
    """Read-only per-target syntax and project checks; no DNS or model requests."""
    try:
        targets = normalize_targets(targets=body.targets)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    project = None
    scope = None
    if body.project_id:
        project = projects_store.find_project(projects_store.load_projects(), body.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="unknown project")
        try:
            scope = projects_store.scope_for_project(project)
        except project_scope.ScopeValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    items = []
    for target in targets:
        try:
            normalized = project_scope.normalize_target(target, body.scan_type)
            decision = project_scope.evaluate_target(scope, target, body.scan_type) if scope else None
            allowed = decision.allowed if decision is not None else True
            items.append({
                "target": target, "valid": True, "allowed": allowed,
                "normalized_target": normalized.value,
                **({"error": decision.reason} if not allowed and decision else {}),
            })
        except project_scope.TargetValidationError as exc:
            items.append({"target": target, "valid": False, "allowed": False, "error": str(exc)})
    return {
        "valid": all(item["valid"] and item["allowed"] for item in items),
        "targets": targets, "target_count": len(targets), "items": items,
        **({"scope_revision": int(project.get("scope_revision") or 1)} if project else {}),
    }


@app.post("/api/scans")
def launch_scan(body: ScanBody) -> dict:
    targets = _requested_targets(body)
    primary_target = targets[0]
    project = _validated_launch_project(body)
    llm_env: dict[str, str] = {}
    if not body.dry_run:
        llm_env = _resolve_llm_env(body)
        errors = validate_model_options(
            llm_env["strix_llm"], llm_env["llm_api_mode"], llm_env["llm_reasoning_effort"]
        )
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))
    run_name = generate_run_name(primary_target, body.scan_type)
    if (state.runs_root / run_name).exists():  # ensure unique
        run_name = f"{run_name[:-RUN_SUFFIX_LENGTH]}{secrets.token_hex(2)}"
    run_dir = state.runs_root / run_name
    (run_dir / "operator_hints").mkdir(parents=True, exist_ok=True)
    (run_dir / ".state").mkdir(parents=True, exist_ok=True)

    if project is not None:
        try:
            project_assignment.write_assignment(run_dir, project["id"], source="console-launch")
        except project_assignment.AssignmentStoreError as exc:
            raise HTTPException(status_code=500, detail=f"project assignment failed: {exc}") from exc

    instruction_file = run_dir / "instruction.md"
    instruction_file.write_text(body.instruction or "# (no instruction)", encoding="utf-8")

    # Launch facts the engine does not put in run.json (dry run, model route)
    # for the cockpit's info surface and the Rerun action. The API key is
    # deliberately NOT persisted — env-only.
    (run_dir / LAUNCH_SIDECAR).write_text(
        json.dumps(
            {
                "target": primary_target,
                "scan_type": body.scan_type,
                **({"targets": targets, "target_count": len(targets)} if len(targets) > 1 else {}),
                "dry_run": bool(body.dry_run),
                "model": llm_env.get("strix_llm") or "",
                "llm_api_mode": resolved_api_mode(llm_env["strix_llm"], llm_env["llm_api_mode"])
                if llm_env
                else "",
                "llm_api_mode_requested": llm_env.get("llm_api_mode") or "",
                "llm_reasoning_effort": llm_env.get("llm_reasoning_effort") or "",
                "profile_id": body.profile_id or "",
                "project_id": body.project_id or "",
                "project_scope_revision": int(project.get("scope_revision") or 1)
                if project is not None
                else None,
                "project_scope_snapshot": projects_store.scope_for_project(project)
                if project is not None
                else None,
            }
        ),
        encoding="utf-8",
    )

    argv = [
        sys.executable,
        "-m",
        "strixops.cli",
        *[arg for target in targets for arg in ("-t", target)],
        "--scan-type",
        body.scan_type,
        "--instruction-file",
        str(instruction_file),
    ]
    if body.crypto:
        argv.append("--crypto")
    if body.socks5:
        argv += ["--socks5", body.socks5]
    if body.gsocket:
        argv += ["--gsocket", body.gsocket]
    if body.dry_run:
        argv.append("--dry-run")

    env = os.environ.copy()
    env["STRIX_RUNS"] = str(state.runs_root)
    env["STRIX_OPERATOR_HINTS_DIR"] = str(run_dir / "operator_hints")
    env["STRIXOPS_RUN_NAME"] = run_name
    env["STRIXOPS_REPORT_LANG"] = "en" if str(body.language).strip().lower().startswith("en") else "zh-CN"

    # Inject the Perplexity key from Settings→Integrations (server-side store)
    # so web_search works without the operator manually setting env.
    integrations = settings_store.load_settings().get("integrations", {})
    perplexity_key = str(integrations.get("perplexity_api_key") or "")
    if perplexity_key:
        env["PERPLEXITY_API_KEY"] = perplexity_key
    env.pop("STRIXOPS_DRY_RUN", None)
    if llm_env:
        env["LLM_API_BASE"] = llm_env["llm_api_base"]
        env["LLM_API_KEY"] = llm_env["llm_api_key"]
        env["STRIX_LLM"] = llm_env["strix_llm"]
        env["LLM_API_MODE"] = llm_env["llm_api_mode"]
        env["LLM_REASONING_EFFORT"] = llm_env["llm_reasoning_effort"]

    # Capture engine stdout+stderr so launch failures are diagnosable; the
    # fd is inherited by the child, the parent's copy can close immediately.
    log_handle = (run_dir / "engine.log").open("ab")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(Path(__file__).resolve().parents[3]),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    (run_dir / ".console.pid").write_text(f"{proc.pid}\n", encoding="utf-8")

    scan_id = uuid.uuid4().hex[:8]
    state.scans[scan_id] = {
        "pid": proc.pid, "popen": proc, "run_name": run_name,
        "target": primary_target, "targets": targets, "target_count": len(targets),
    }
    return {"ok": True, "scan_id": scan_id, "run_name": run_name, "pid": proc.pid}


@app.get("/api/scans")
def list_scans() -> dict:
    scans = []
    for scan_id, info in state.scans.items():
        popen = info.get("popen")
        scans.append(
            {
                "scan_id": scan_id,
                "pid": info.get("pid"),
                "run_name": info.get("run_name"),
                "target": info.get("target"),
                "targets": info.get("targets", [info.get("target")]),
                "target_count": info.get("target_count", 1),
                "alive": _pid_alive(info.get("pid")),
                "exit_code": popen.poll() if popen is not None else None,
            }
        )
    return {"scans": scans}


# ---------------------------------------------------------------- projects


class ProjectBody(BaseModel):
    name: str = ""
    description: str = ""
    color: str = "gold"
    scope_rules: list[dict[str, Any]] | None = None


class ProjectScopeBody(BaseModel):
    scope_rules: list[dict[str, Any]]


class ProjectTargetBody(BaseModel):
    target: str
    scan_type: Literal["web", "internal"]


class ProjectReportBody(BaseModel):
    language: str = "zh-CN"


@app.get("/api/projects")
def list_projects() -> dict:
    return projects_store.project_summaries()


@app.post("/api/projects")
def create_project(body: ProjectBody) -> dict:
    data = projects_store.load_projects()
    project, errors = projects_store.sanitize_project(body.model_dump(exclude_none=True))
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    data["projects"].append(project)
    projects_store.save_projects(data)
    return {"ok": True, "project": projects_store.public_project(project)}


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, body: ProjectBody) -> dict:
    data = projects_store.load_projects()
    existing = projects_store.find_project(data, project_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="unknown project")
    project, errors = projects_store.sanitize_project(body.model_dump(exclude_none=True), existing=existing)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    data["projects"] = [project if p.get("id") == project_id else p for p in data["projects"]]
    projects_store.save_projects(data)
    return {"ok": True, "project": projects_store.public_project(project)}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict:
    data = projects_store.load_projects()
    if projects_store.find_project(data, project_id) is None:
        raise HTTPException(status_code=404, detail="unknown project")
    assigned_runs = projects_store.project_runs(project_id)
    data["projects"] = [p for p in data["projects"] if p.get("id") != project_id]
    projects_store.save_projects(data)
    cleared = 0
    for run_dir in assigned_runs:
        try:
            project_assignment.clear_assignment(run_dir, source="project-delete")
            state.summary_cache.pop(run_dir.name, None)
            cleared += 1
        except project_assignment.AssignmentStoreError:
            # The deleted id no longer resolves and is therefore treated as
            # unassigned even if one corrupt sidecar cannot be rewritten.
            continue
    return {"ok": True, "unassigned_runs": cleared}


@app.post("/api/projects/{project_id}/validate-target")
def validate_project_target(project_id: str, body: ProjectTargetBody) -> dict:
    """Preview the launch-time scope decision without resolving or launching a target."""
    data = projects_store.load_projects()
    project = projects_store.find_project(data, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="unknown project")
    try:
        decision = project_scope.evaluate_target(
            projects_store.scope_for_project(project),
            body.target,
            body.scan_type,
        )
    except (project_scope.ScopeValidationError, project_scope.TargetValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "allowed": decision.allowed,
        "normalized_target": decision.target.value,
        "reason": decision.reason,
        "scope_revision": project["scope_revision"],
    }


@app.put("/api/projects/{project_id}/scope")
def update_project_scope(project_id: str, body: ProjectScopeBody) -> dict:
    data = projects_store.load_projects()
    project = projects_store.find_project(data, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="unknown project")

    mode = "unrestricted" if projects_store._is_any_rules(body.scope_rules) else "restricted"
    raw_scope = {"schema_version": 1, "mode": mode, "entries": body.scope_rules}
    try:
        normalized = project_scope.normalize_scope(raw_scope)
    except project_scope.ScopeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    active_conflicts: list[str] = []
    historical_conflicts: list[str] = []
    for run_dir in projects_store.project_runs(project_id):
        summary = _run_summary(run_dir, cache=False)
        try:
            _assert_run_scope(normalized, summary)
            allowed = True
        except (project_scope.ScopeError, ValueError):
            allowed = False
        if allowed:
            continue
        if summary.get("live"):
            active_conflicts.append(run_dir.name)
        else:
            historical_conflicts.append(run_dir.name)

    if active_conflicts:
        names = ", ".join(active_conflicts[:5])
        suffix = "…" if len(active_conflicts) > 5 else ""
        raise HTTPException(
            status_code=409,
            detail=f"scope excludes active tasks: {names}{suffix}",
        )

    previous = projects_store.scope_for_project(project)
    if previous != normalized:
        project["scope_revision"] = int(project.get("scope_revision") or 1) + 1
    project["scope"] = normalized
    project["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    projects_store.save_projects(data)
    summary = next(
        (item for item in projects_store.project_summaries()["projects"] if item.get("id") == project_id),
        projects_store.public_project(project),
    )
    return {
        "ok": True,
        "project": summary,
        "historical_out_of_scope": historical_conflicts,
    }


@app.get("/api/projects/{project_id}/runs")
def project_runs_endpoint(project_id: str) -> dict:
    summaries = projects_store.project_summaries()
    project = next(
        (item for item in summaries["projects"] if item.get("id") == project_id),
        None,
    )
    if project is None:
        raise HTTPException(status_code=404, detail="unknown project")
    runs = []
    for run_dir in projects_store.project_runs(project_id):
        summary = _run_summary(run_dir, cache=False)
        try:
            _assert_run_scope(projects_store.scope_for_project(project), summary)
            summary["scope_match"] = True
        except (project_scope.ScopeError, ValueError):
            summary["scope_match"] = False
        runs.append(summary)
    runs.sort(key=lambda r: r.get("start_time") or "", reverse=True)
    return {"project": projects_store.public_project(project), "runs": runs}


@app.get("/api/projects/{project_id}/findings")
def project_findings_endpoint(project_id: str) -> dict:
    data = projects_store.load_projects()
    if projects_store.find_project(data, project_id) is None:
        raise HTTPException(status_code=404, detail="unknown project")
    return projects_store.project_findings(project_id)


@app.get("/api/projects/{project_id}/report")
def project_report_endpoint(project_id: str) -> dict:
    data = projects_store.load_projects()
    project = projects_store.find_project(data, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="unknown project")
    latest = project_reports.read_project_report(
        project_id,
        run_dirs=projects_store.project_runs(project_id),
        project=project,
    )
    if latest is not None and latest.get("content"):
        return {"markdown": latest["content"], "report": latest}
    return {"markdown": projects_store.project_report_markdown(project), "report": None}


@app.get("/api/projects/{project_id}/reports")
def list_project_reports_endpoint(project_id: str) -> dict:
    data = projects_store.load_projects()
    project = projects_store.find_project(data, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="unknown project")
    run_dirs = projects_store.project_runs(project_id)
    reports = project_reports.list_project_reports(
        project_id,
        run_dirs=run_dirs,
        project=project,
    )
    status = project_reports.project_report_status(
        project_id,
        run_dirs,
        project=project,
    )
    return {"reports": reports, "stale": bool(status.get("stale")), "status": status}


@app.post("/api/projects/{project_id}/reports")
def generate_project_report_endpoint(project_id: str, body: ProjectReportBody) -> dict:
    data = projects_store.load_projects()
    project = projects_store.find_project(data, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="unknown project")
    run_dirs = projects_store.project_runs(project_id)
    try:
        generated = project_reports.generate_project_report(
            project,
            run_dirs,
            language=body.language,
        )
        report = project_reports.read_project_report(
            project_id,
            generated["version"],
            run_dirs=run_dirs,
            project=project,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "report": report or generated}


@app.get("/api/projects/{project_id}/reports/{version}")
def read_project_report_endpoint(project_id: str, version: str) -> dict:
    data = projects_store.load_projects()
    project = projects_store.find_project(data, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="unknown project")
    try:
        report = project_reports.read_project_report(
            project_id,
            version,
            run_dirs=projects_store.project_runs(project_id),
            project=project,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if report is None:
        raise HTTPException(status_code=404, detail="unknown project report")
    return {"report": report}


@app.get("/api/projects/{project_id}/analytics/skills")
def project_skill_analytics(project_id: str) -> dict:
    data = projects_store.load_projects()
    if projects_store.find_project(data, project_id) is None:
        raise HTTPException(status_code=404, detail="unknown project")
    return project_skills.aggregate_project_skill_analytics(projects_store.project_runs(project_id))


class AssignBody(BaseModel):
    project_id: str = ""


@app.post("/api/runs/{name}/assign")
def assign_run_to_project(name: str, body: AssignBody) -> dict:
    run_dir = state.run_dir(name)
    if body.project_id:
        data = projects_store.load_projects()
        project = projects_store.find_project(data, body.project_id)
        if project is None:
            raise HTTPException(status_code=400, detail="assign failed (unknown project)")
        summary = _run_summary(run_dir, cache=False)
        try:
            _assert_run_scope(projects_store.scope_for_project(project), summary)
        except (project_scope.ScopeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not projects_store.assign_run(run_dir, body.project_id):
        raise HTTPException(status_code=400, detail="assign failed (unknown project or unreadable run)")
    state.summary_cache.pop(name, None)
    return {"ok": True}


# ------------------------------------------------------- knowledge-base files


def _content_root() -> Path:
    import strixops.skills as skills_pkg

    return Path(skills_pkg.CONTENT_ROOT).resolve()


class SkillWriteBody(BaseModel):
    content: str


@app.get("/api/skills")
def list_skill_files() -> dict:
    root = _content_root()
    files = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        files.append(
            {
                "path": rel,
                "category": rel.split("/")[0] if "/" in rel else "",
                "name": path.stem,
                "size": path.stat().st_size,
                "modified": path.stat().st_mtime,
            }
        )
    return {"files": files, "root": str(root)}


@app.get("/api/skills/file")
def read_skill_file(path: str) -> dict:
    target = (_content_root() / path).resolve()
    if not target.is_relative_to(_content_root()) or not target.is_file():
        raise HTTPException(status_code=404, detail="unknown skill file")
    return {"path": path, "content": target.read_text(encoding="utf-8", errors="replace")}


@app.put("/api/skills/file")
def write_skill_file(path: str, body: SkillWriteBody) -> dict:
    target = (_content_root() / path).resolve()
    if not target.is_relative_to(_content_root()) or target.suffix != ".md":
        raise HTTPException(status_code=400, detail="invalid skill path")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(target)
    return {"ok": True, "path": path}


class PromptWriteBody(BaseModel):
    content: str


@app.get("/api/prompts")
def list_prompt_files() -> dict:
    from strixops.agents import prompts as prompts_mod

    return {"parts": prompts_mod.list_prompt_parts(), "root": str(prompts_mod.PROMPT_PARTS_DIR)}


@app.get("/api/prompts/{name}")
def read_prompt_file(name: str) -> dict:
    from strixops.agents import prompts as prompts_mod

    content = prompts_mod.read_prompt_part(name)
    if content is None:
        raise HTTPException(status_code=404, detail="unknown prompt part")
    return {"name": name, "content": content}


@app.put("/api/prompts/{name}")
def write_prompt_file(name: str, body: PromptWriteBody) -> dict:
    from strixops.agents import prompts as prompts_mod

    if not prompts_mod.write_prompt_part(name, body.content):
        raise HTTPException(status_code=400, detail="invalid prompt part name")
    return {"ok": True, "name": name}


# ------------------------------------------------------------- skill analytics


@app.get("/api/analytics/skills")
def skill_analytics() -> dict:
    """Use the same load/injection accounting as project workspaces."""
    analytics = project_skills.aggregate_project_skill_analytics(_iter_run_dirs())
    # Insights historically plots only tasks that have used skills.  Preserve
    # that sample set while the project workspace includes all project tasks.
    return {
        "skills": analytics["skills"],
        "categories": analytics["categories"],
        "runs": [run for run in analytics["runs"] if run["total"] > 0],
        "totals": {
            key: analytics["totals"][key] for key in ("distinct_skills", "total_loads", "runs_with_skills")
        },
    }


# ------------------------------------------------------------- system prompt


@app.get("/api/runs/{name}/prompts")
def run_prompts(name: str) -> dict:
    """List saved system-prompt snapshots for a run."""
    run_dir = state.run_dir(name)
    state_dir = run_dir / ".state"
    if not state_dir.is_dir():
        return {"prompts": []}
    prompts = []
    for path in sorted(state_dir.glob("prompt_*.md")):
        # prompt_root.md → "root", prompt_scanner.md → "scanner"
        agent = path.stem.replace("prompt_", "", 1)
        prompts.append(
            {
                "agent": agent,
                "file": path.name,
                "size": path.stat().st_size,
                "url": f"/api/runs/{quote(name)}/prompts/{quote(path.name)}",
            }
        )
    return {"prompts": prompts}


@app.get("/api/runs/{name}/prompts/{filename}")
def run_prompt_file(name: str, filename: str) -> FileResponse:
    run_dir = state.run_dir(name)
    target = (run_dir / ".state" / filename).resolve()
    if not target.is_relative_to(run_dir.resolve()) or not target.is_file():
        raise HTTPException(status_code=404, detail="prompt snapshot not found")
    return FileResponse(target, media_type="text/markdown")


# ---------------------------------------------------------------- evidence


_CATEGORY_LABELS = {
    "credential_dump": "Credential Dump",
    "credential": "Credentials",
    "ssh_key": "SSH Keys",
    "cloud_credential": "Cloud Credentials",
    "crypto": "Crypto Assets",
    "config": "Configuration",
    "recon_data": "Recon Data",
    "database": "Database",
    "data": "Data Files",
    "other": "Other",
}


def _size_human(size: int) -> str:
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024 * 1024):.1f} GB"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _evidence_entries(run_dir: Path) -> list[dict[str, Any]]:
    """Read a trusted manifest without following paths supplied by the workspace.

    Legacy entries without delivery flags are usable only when the attachment
    still exists as a regular file. Explicit failed delivery is never upgraded.
    """
    try:
        with os.fdopen(
            _open_run_file(run_dir, "evidence/.evidence_index.json"), "r", encoding="utf-8"
        ) as source:
            raw = json.load(source)
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    entries = []
    seen = set()
    for value in raw:
        if not isinstance(value, dict) or not isinstance(value.get("filename"), str):
            continue
        try:
            parts = _relative_file_parts(value["filename"])
        except ValueError:
            continue
        filename = Path(*parts).as_posix()
        if parts[-1] == ".evidence_index.json" or filename in seen:
            continue
        seen.add(filename)
        entry = dict(value, filename=filename)
        entry.pop("download_url", None)
        delivered = entry.get("deliverable", entry.get("persisted", True)) is True
        try:
            with os.fdopen(_open_run_file(run_dir, f"evidence/{filename}"), "rb") as source:
                info = os.fstat(source.fileno())
            if isinstance(entry.get("size"), int) and entry["size"] != info.st_size:
                raise OSError("evidence size changed after collection")
            entry.setdefault("size", info.st_size)
        except (OSError, ValueError) as exc:
            if delivered:
                entry["error"] = f"Attachment unavailable: {exc}"
            delivered = False
        entry["deliverable"] = delivered
        entry["size"] = entry["size"] if isinstance(entry.get("size"), int) and entry["size"] >= 0 else 0
        entry["category"] = str(entry.get("category") or "other")
        entry["sha256"] = str(entry.get("sha256") or "")
        entries.append(entry)
    return entries


@app.get("/api/runs/{name}/evidence")
def run_evidence(name: str) -> dict:
    entries = _evidence_entries(state.run_dir(name))

    for entry in entries:
        entry["size_human"] = _size_human(entry.get("size", 0))
        entry["category_label"] = _CATEGORY_LABELS.get(entry.get("category", ""), "Other")
        sha = entry.get("sha256", "")
        entry["sha256_short"] = sha[:8] + "…" if len(sha) > 8 else sha
        if entry["deliverable"]:
            entry["download_url"] = f"/api/runs/{quote(name, safe='')}/evidence/{quote(entry['filename'])}"

    return {
        "evidence": entries,
        "totals": {
            "count": len(entries),
            "total_bytes": sum(e.get("size", 0) for e in entries),
            "total_human": _size_human(sum(e.get("size", 0) for e in entries)),
        },
    }


@app.get("/api/runs/{name}/evidence/{file_path:path}")
def run_evidence_file(name: str, file_path: str, request: Request) -> Response:
    run_dir = state.run_dir(name)
    try:
        filename = Path(*_relative_file_parts(file_path)).as_posix()
        if not any(
            entry["filename"] == filename and entry["deliverable"] for entry in _evidence_entries(run_dir)
        ):
            raise ValueError("attachment is not deliverable")
        fd = _open_run_file(run_dir, f"evidence/{filename}")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="evidence file not found") from exc
    return _file_stream(fd, "application/octet-stream", request)


# ----------------------------------------------------------------- static UI


class _StaticExportFiles(StaticFiles):
    """``StaticFiles(html=True)`` plus Next static-export clean URLs.

    Starlette's html mode maps ``dir/`` → ``index.html`` and serves
    ``404.html`` on a miss, but the export ships flat ``run.html`` /
    ``scan.html`` files — ``/run`` must resolve to ``run.html`` before the
    404 fallback wins.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        if scope.get("path", "").startswith("/api/"):
            # Unknown API paths must 404 as JSON, not fall through to the UI.
            raise StarletteHTTPException(status_code=404)
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException:
            # No 404.html fallback — still retry the clean-URL form first.
            response = None
        if response is not None and response.status_code != 404:
            return response
        if "." not in os.path.basename(path):
            # Suppress BOTH exception classes: StaticFiles raises starlette's
            # own HTTPException (the parent of fastapi's).
            with contextlib.suppress(StarletteHTTPException):
                return await super().get_response(f"{path}.html", scope)
        if response is not None:
            return response
        raise StarletteHTTPException(status_code=404)


def _mount_static(web_dist: Path) -> None:
    """Serve the built UI at ``/`` — mounted AFTER the API routes, so the API wins."""
    if not web_dist.is_dir():
        return
    app.mount("/", _StaticExportFiles(directory=web_dist, html=True), name="console")


def _web_dist_path() -> Path:
    """Resolve packaged console assets first, then the source-checkout export."""
    packaged = Path(__file__).resolve().parent / "web"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[3] / "console" / "web" / "out"


def main() -> int:
    global state
    args_parser = argparse.ArgumentParser(prog="strixops-console")
    args_parser.add_argument("--runs-root", default="")
    args_parser.add_argument("--host", default="127.0.0.1")
    args_parser.add_argument("--port", type=int, default=8300)
    args = args_parser.parse_args()

    state = ConsoleState(_runs_root_from(args.runs_root))
    _mount_static(_web_dist_path())

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
