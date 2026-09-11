"""Console adapters for independent, queued target assessments.

Private launch snapshots stay outside the run/artifact tree. The scheduler sees
only their opaque references; each child receives an ordinary single-target run.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from strixops import skills
from strixops.agents import prompts
from strixops.config.model_options import resolved_api_mode
from strixops.console import parser, project_assignment, project_scope, projects_store
from strixops.engine.scanconfig import ScanSpec
from strixops.queue import (
    BatchScheduler,
    LaunchReceipt,
    QueueError,
    QueueStore,
    RunObservation,
    process_alive,
    process_identity,
)

_REFERENCE = re.compile(r"[a-f0-9]{32}\Z")
_RUN_NAME = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_MAX_SNAPSHOT = 32 * 1024 * 1024


def _write_private(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class LaunchSnapshots:
    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, reference: str) -> Path:
        if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
            raise QueueError("invalid_snapshot", "The saved launch configuration is invalid.")
        return self.directory / f"{reference}.json"

    def save(self, data: dict) -> str:
        reference = uuid.uuid4().hex
        _write_private(self._path(reference), {"schema_version": 1, **data})
        return reference

    def read(self, reference: str) -> dict:
        path = self._path(reference)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SNAPSHOT:
                    raise ValueError
                data = json.loads(handle.read(_MAX_SNAPSHOT + 1))
            if not isinstance(data, dict) or data.get("schema_version") != 1:
                raise ValueError
            return data
        except (OSError, ValueError, RecursionError) as exc:
            raise QueueError(
                "snapshot_unavailable", "The saved launch configuration is unavailable.",
            ) from exc

    def delete(self, reference: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path(reference).unlink()


def _capture_resources() -> dict:
    return {
        "prompt_parts": {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(prompts.PROMPT_PARTS_DIR.glob("*.md"))
        },
        "skills": skills.snapshot_skills(),
    }


def _publish_resources(run_dir: Path, resources: dict, spec: ScanSpec) -> None:
    """Keep the existing PromptResources format, with this target's own scope."""
    payload = {"schema_version": 1, **resources, "spec": asdict(spec)}
    path = run_dir / ".state" / "prompt_resources.json"
    _write_private(path, payload)
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    _write_private(run_dir / ".state" / "prompt_manifest.json", {
        "schema_version": 1,
        "resources_file": path.name,
        "resources_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "prompt_parts": {name: digest(text) for name, text in resources["prompt_parts"].items()},
        "skills": {row["id"]: digest(row["content"]) for row in resources["skills"]},
        "agents": [],
    })


class ConsoleBatchController:
    def __init__(self, store: QueueStore, runs_root: Path, register: Callable[[dict, Any], None]):
        self.store = store
        self.runs_root = runs_root.resolve()
        self.owner = "console:" + str(self.runs_root)
        self.snapshots = LaunchSnapshots(store.path.parent / "scan_queue_snapshots")
        self.register = register
        self.processes: dict[str, Any] = {}
        self.scheduler = BatchScheduler(
            store, launch=self.launch, inspect=self.inspect, cancel=self.cancel,
            validate_launch=self.validate_launch,
            owner=self.owner,
        )

    def create(self, body: dict, *, targets: list[str], llm_env: dict, search_env: dict,
               sources: dict[str, dict] | None = None) -> dict:
        # No raw inline credential is needed in public batch metadata.
        scan = {key: body.get(key) for key in (
            "scan_type", "crypto", "socks5", "gsocket", "instruction", "dry_run",
            "profile_id", "language", "project_id",
        )}
        reference = self.snapshots.save({
            "scan": scan, "llm_env": llm_env, "search_env": search_env,
            "resources": _capture_resources(), "sources": sources or {},
            "runs_root": str(self.runs_root),
        })
        try:
            return self.store.create_batch(
                targets=targets, scan_type=body.get("scan_type") or "web",
                snapshot_ref=reference, name=body.get("name") or "",
                max_concurrent=body.get("max_concurrent", 2),
                project_id=body.get("project_id") or "",
                source={"kind": "fofa"} if sources else None,
                owner=self.owner,
            )
        except BaseException:
            self.snapshots.delete(reference)
            raise

    def _snapshot(self, item: dict) -> dict:
        snapshot = self.snapshots.read(item["snapshot_ref"])
        if Path(snapshot.get("runs_root", "")).resolve() != self.runs_root:
            raise QueueError("different_runs_root", "This batch belongs to another task directory.")
        return snapshot

    def validate_launch(self, item: dict) -> None:
        snapshot = self._snapshot(item)
        scan = snapshot["scan"]
        try:
            project_scope.normalize_target(item["target"], item["scan_type"])
            if scan.get("project_id"):
                project = projects_store.find_project(projects_store.load_projects(), scan["project_id"])
                if project is None:
                    raise ValueError
                project_scope.assert_target_allowed(
                    projects_store.scope_for_project(project), item["target"], item["scan_type"],
                )
        except (ValueError, project_scope.TargetOutOfScopeError) as exc:
            raise QueueError("scope_changed", "The target no longer matches its project scope.") from exc

    def _run_dir(self, item: dict) -> Path:
        name = item.get("run_name")
        if not isinstance(name, str) or not _RUN_NAME.fullmatch(name):
            raise QueueError("invalid_run", "The queued task identity is invalid.")
        path = self.runs_root / name
        if path.is_symlink():
            raise QueueError("invalid_run", "The queued task directory is invalid.")
        return path

    def launch(self, item: dict) -> LaunchReceipt:
        try:
            process, run_dir = self._spawn(item)
        except Exception as exc:
            # Everything in _spawn either prepares files or calls Popen. A
            # Popen failure leaves no running child to reserve capacity for.
            raise QueueError("queue_launch_failed") from exc
        self.processes[item["id"]] = process
        # Once spawned, always return the receipt even if an optional sidecar fails.
        with contextlib.suppress(OSError):
            (run_dir / ".console.pid").write_text(str(process.pid), encoding="utf-8")
        with contextlib.suppress(Exception):
            self.register(item, process)
        return LaunchReceipt(
            pid=process.pid, run_name=item["run_name"], run_dir=str(run_dir),
            start_identity=process_identity(process.pid),
        )

    def _spawn(self, item: dict) -> tuple[Any, Path]:
        snapshot = self._snapshot(item)
        scan, llm = snapshot["scan"], snapshot["llm_env"]
        run_dir = self._run_dir(item)
        # A reserved name can be launched once. Existing data is never overwritten.
        run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        (run_dir / ".state").mkdir(mode=0o700)
        (run_dir / "operator_hints").mkdir(mode=0o700)
        instruction_file = run_dir / "instruction.md"
        instruction_file.write_text(scan.get("instruction") or "# (no instruction)", encoding="utf-8")
        project = None
        if scan.get("project_id"):
            project = projects_store.find_project(projects_store.load_projects(), scan["project_id"])
            if project is None:
                raise QueueError("scope_changed", "The project is no longer available.")
            project_assignment.write_assignment(run_dir, project["id"], source="console-batch")
        language = "en" if scan.get("language") == "en" else "zh-CN"
        spec = ScanSpec(
            target=item["target"], scan_type=item["scan_type"], crypto=bool(scan.get("crypto")),
            instruction_file=str(instruction_file), instruction_text=scan.get("instruction") or "",
            socks5_proxy=scan.get("socks5") or "", gsocket_key=scan.get("gsocket") or "",
            report_language=language,
        )
        _publish_resources(run_dir, snapshot["resources"], spec)
        source = snapshot.get("sources", {}).get(item["target"])
        metadata = {
            "target": item["target"], "scan_type": item["scan_type"], "engine": "ops",
            "dry_run": bool(scan.get("dry_run")), "model": llm.get("strix_llm", ""),
            "llm_api_mode": resolved_api_mode(llm["strix_llm"], llm["llm_api_mode"]) if llm else "",
            "llm_api_mode_requested": llm.get("llm_api_mode", ""),
            "llm_reasoning_effort": llm.get("llm_reasoning_effort", ""),
            "profile_id": scan.get("profile_id") or "", "project_id": scan.get("project_id") or "",
            "project_scope_revision": int(project.get("scope_revision") or 1) if project else None,
            "project_scope_snapshot": projects_store.scope_for_project(project) if project else None,
            "batch_id": item["batch_id"], "batch_item_id": item["id"],
            **({"source": source} if source else {}),
        }
        _write_private(run_dir / ".console_launch.json", metadata)
        argv = [sys.executable, "-m", "strixops.cli", "-t", item["target"],
                "--scan-type", item["scan_type"], "--instruction-file", str(instruction_file)]
        for key, flag in (("socks5", "--socks5"), ("gsocket", "--gsocket")):
            if scan.get(key):
                argv.extend([flag, scan[key]])
        if scan.get("crypto"):
            argv.append("--crypto")
        if scan.get("dry_run"):
            argv.append("--dry-run")
        env = os.environ.copy()
        env.update(snapshot.get("search_env") or {})
        env.update({
            "STRIX_RUNS": str(self.runs_root), "STRIXOPS_RUN_NAME": item["run_name"],
            "STRIX_OPERATOR_HINTS_DIR": str(run_dir / "operator_hints"),
            "STRIXOPS_REPORT_LANG": language, "STRIXOPS_QUEUE_DB": str(self.store.path),
            "STRIXOPS_QUEUE_ITEM_ID": item["id"], "STRIXOPS_QUEUE_ITEM_TOKEN": item["launch_token"],
            "STRIX_HOST_WORKSPACE_DIR": str(run_dir / "workspace"),
        })
        env.pop("STRIXOPS_DRY_RUN", None)
        for source_key, destination in (
            ("llm_api_base", "LLM_API_BASE"), ("llm_api_key", "LLM_API_KEY"),
            ("strix_llm", "STRIX_LLM"), ("llm_api_mode", "LLM_API_MODE"),
            ("llm_reasoning_effort", "LLM_REASONING_EFFORT"),
        ):
            if source_key in llm:
                env[destination] = llm[source_key]
        with (run_dir / "engine.log").open("ab") as handle:
            process = subprocess.Popen(
                argv, cwd=str(Path(__file__).resolve().parents[3]), env=env,
                stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
            )
        return process, run_dir

    def inspect(self, item: dict) -> RunObservation:
        process = self.processes.get(item["id"])
        alive = process.poll() is None if process is not None else process_alive(
            item.get("pid"), item.get("start_identity") or "",
        )
        run_dir = self._run_dir(item)
        record = parser.json_load(run_dir / "run.json")
        cleanup = record.get("cleanup") or {}
        sandbox = cleanup.get("sandbox") or {}
        no_sandbox = not sandbox and bool((record.get("scan_config") or {}).get("dry_run"))
        cleanup_complete = cleanup.get("status") == "complete" or (
            sandbox.get("verified") is True and sandbox.get("status") == "removed"
        ) or (no_sandbox and cleanup.get("status") in {"complete", "failed"})
        queue_cleanup = (record.get("queue") or {}).get("cleanup_complete")
        if isinstance(queue_cleanup, bool):
            cleanup_complete = queue_cleanup
        return RunObservation(
            alive=alive, status=record.get("status") or "starting",
            cleanup_complete=cleanup_complete,
            report_ready=(run_dir / "penetration_test_report.md").is_file(),
            error_code="run_failed" if record.get("status") == "failed" else "",
        )

    def cancel(self, item: dict) -> None:
        pid, identity = item.get("pid"), item.get("start_identity") or ""
        process = self.processes.get(item["id"])
        if process is not None:
            if process.poll() is not None:
                return
            pid = process.pid
            identity = process_identity(pid)
        if not pid or not identity or process_alive(pid, identity) is not True:
            return
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
