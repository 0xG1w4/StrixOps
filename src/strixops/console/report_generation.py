"""Durable, independently retryable model reports from saved run artifacts.

Only the report model runs here. Scan tools, lifecycle methods and RunState.save
are deliberately unavailable on the read-only source snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import fcntl
import json
import logging
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from strixops.config.provider import make_platform_model
from strixops.config.settings import EngineSettings
from strixops.console import settings_store
from strixops.console.model_catalog import normalize_api_base
from strixops.platform import artifacts
from strixops.report.assessment import empty_assessment
from strixops.report.diagnostics import MESSAGES
from strixops.report.synthesis import synthesize_executive_report

logger = logging.getLogger(__name__)
REPORT = "penetration_test_report.md"
STATUS = ".state/report-generation.json"
LOCK = ".state/report-generation.lock"
_tasks: dict[str, asyncio.Task] = {}
_SAFE_ERRORS = {
    "interrupted": "Report generation was interrupted by a Console restart. Generate the report again.",
    "model_unavailable": (
        "No usable model profile is available. Configure a model and API key in Settings, then retry."
    ),
    "source_error": (
        "Saved report sources are missing, unreadable or invalid. Check this run's saved artifacts."
    ),
    "storage_error": (
        "The report could not be saved. Check free disk space and run-directory permissions, then retry."
    ),
    "model_error": (
        "The model did not produce a final report. Check the model connection in Settings and retry."
    ),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class GenerationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_SAFE_ERRORS[code])


@dataclass
class _Lease:
    descriptor: int | None

    def close(self) -> None:
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is not None:
            os.close(descriptor)


def _read(run_dir: Path, relative: str, *, limit: int = 32 * 1024 * 1024) -> str:
    """Read regular run-owned files, refusing symlinks at every component."""
    parts = Path(relative).parts
    if Path(relative).is_absolute() or ".." in parts:
        raise ValueError("invalid path")
    descriptors: list[int] = []
    try:
        directory = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("not a regular file")
            data = source.read(limit + 1)
        if len(data) > limit:
            raise ValueError("source exceeds read limit")
        return data.decode("utf-8")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _json(run_dir: Path, relative: str, *, default: Any = None) -> Any:
    try:
        text = _read(run_dir, relative)
    except FileNotFoundError:
        if default is not None:
            return copy.deepcopy(default)
        raise

    def reject(value: str) -> None:
        raise ValueError("invalid JSON number")

    return json.loads(text, parse_constant=reject)


def _write_status(run_dir: Path, generation: dict) -> None:
    artifacts.atomic_write_text(run_dir / STATUS, json.dumps(generation, ensure_ascii=False, indent=2))


def _acquire(run_dir: Path) -> int | None:
    if run_dir.is_symlink():
        raise OSError("run directory is a symlink")
    state_dir = run_dir / ".state"
    state_dir.mkdir(exist_ok=True)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise OSError("state directory is not a run-owned directory")
    descriptor = os.open(run_dir / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("lock is not a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_generation(run_dir: Path) -> dict[str, Any]:
    generation = _json(run_dir, STATUS, default={"status": "idle"})
    if not isinstance(generation, dict) or generation.get("status") not in {
        "idle", "running", "completed", "failed",
    }:
        raise ValueError("invalid generation record")
    return generation


def status(run_dir: Path) -> dict[str, Any]:
    """Recover a prior process's unfinished job once its OS lock is released."""
    try:
        generation = _read_generation(run_dir)
        if generation["status"] == "running":
            descriptor = _acquire(run_dir)
            if descriptor is not None:
                try:
                    # The job can finish between the unlocked read and flock.
                    # Recover only the latest record while holding its lease;
                    # a completed/failed result or a newer job must survive.
                    generation = _read_generation(run_dir)
                    if generation["status"] == "running":
                        record = _json(run_dir, "run.json")
                        if (
                            isinstance(record, dict) and record.get("report_synthesized") is True
                            and record.get("report_generation_id") == generation.get("job_id")
                            and _read(run_dir, REPORT)
                        ):
                            generation.update(status="completed", updated_at=_now(), completed_at=_now())
                            generation.pop("code", None)
                            generation.pop("error", None)
                        else:
                            generation.update(
                                status="failed", code="interrupted", error=_SAFE_ERRORS["interrupted"],
                                updated_at=_now(), completed_at=_now(),
                            )
                        _write_status(run_dir, generation)
                finally:
                    os.close(descriptor)
        if generation["status"] == "idle":
            record = _json(run_dir, "run.json", default={})
            previous = _json(run_dir, ".state/report-synthesis.json", default={})
            if not previous and isinstance(record, dict):
                previous = record.get("report_synthesis") or {}
            if isinstance(previous, dict) and previous.get("status") in {"failed", "skipped"}:
                generation["code"] = previous.get("code")
        # Publish only fixed messages for known diagnostic codes. Saved files or
        # provider payloads can never introduce raw errors into this API.
        projected = {key: value for key, value in generation.items() if key in {
            "status", "job_id", "started_at", "updated_at", "completed_at", "model",
        } and isinstance(value, str)}
        code = generation.get("code")
        if isinstance(code, str) and code in MESSAGES | _SAFE_ERRORS:
            projected.update(code=code, error=(_SAFE_ERRORS | MESSAGES)[code])
        return projected
    except (OSError, UnicodeError, ValueError):
        return {"status": "failed", "code": "storage_error", "error": _SAFE_ERRORS["storage_error"]}


@dataclass
class SavedAssessment:
    data: dict[str, Any]

    def snapshot(self, _status: str) -> dict[str, Any]:
        return copy.deepcopy(self.data)


@dataclass
class SavedReportState:
    run_dir: Path
    run_record: dict[str, Any]
    reports: list[dict[str, Any]]
    internal_findings: list[dict[str, Any]]
    assessment: SavedAssessment
    final_fields: dict[str, Any] | None

    def duration_seconds(self) -> int:
        return int(self.run_record.get("duration_seconds") or 0)

    def report_language(self) -> str:
        return str(self.run_record["scan_config"].get("report_language") or "zh-CN")


def load_source(run_dir: Path) -> SavedReportState:
    try:
        record = _json(run_dir, "run.json")
        if not isinstance(record, dict) or not isinstance(record.get("scan_config"), dict):
            raise ValueError("invalid run record")
        reports = _json(run_dir, "vulnerabilities.json", default=[])
        if not isinstance(reports, list) or not all(isinstance(row, dict) for row in reports):
            raise ValueError("invalid vulnerability records")
        internal: list[dict[str, Any]] = []
        directory = run_dir / "internal_findings"
        if directory.is_symlink():
            raise ValueError("unsafe findings directory")
        paths = sorted(directory.glob("*.md"))
        if len(paths) > 2000:
            raise ValueError("too many findings")
        for path in paths:
            markdown = _read(run_dir, f"internal_findings/{path.name}")
            finding: dict[str, Any] = {"id": path.stem, "content": markdown}
            header = markdown.split("\n## Details", 1)[0]
            for line in header.splitlines():
                if line.startswith("# "):
                    finding.setdefault("title", line[2:].strip())
                match = re.match(r"\*\*(ID|Type|Host|Source|Severity):\*\*\s*(.*)", line)
                if match:
                    key = {"Type": "finding_type"}.get(match[1], match[1].lower())
                    finding[key] = match[2]
            metadata = re.search(r"\n## Metadata\s*\n```json\s*\n(.*)\n```\s*$", markdown, re.S)
            if metadata:
                with contextlib.suppress(ValueError):
                    finding["metadata"] = json.loads(metadata[1])
            internal.append(finding)
        assessment = _json(
            run_dir, "assessment.json", default=empty_assessment(record.get("status", "unknown")),
        )
        if not isinstance(assessment, dict):
            raise ValueError("invalid assessment")
        draft = record.get("scan_results")
        if draft is not None and not isinstance(draft, dict):
            raise ValueError("invalid root narrative")
        if not reports and not internal and not draft:
            raise ValueError("no saved findings or root narrative")
        return SavedReportState(run_dir, record, reports, internal, SavedAssessment(assessment), draft)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise GenerationError("source_error") from exc


def model_settings(run_dir: Path, source: SavedReportState) -> EngineSettings:
    """Use a current saved profile's endpoint and key together, never run metadata."""
    try:
        launch = _json(run_dir, ".console_launch.json", default={})
        data = settings_store.load_settings()
        profile_id = launch.get("profile_id") if isinstance(launch, dict) else None
        profiles = {p.get("id"): p for p in data["profiles"]}
        profile = profiles.get(profile_id) or profiles.get(data["active_profile_id"])
        if profile is None:
            settings = EngineSettings.from_env()
        else:
            resolved = settings_store.effective_llm(
                profile, source.run_record["scan_config"].get("scan_type", "web"),
            )
            settings = EngineSettings(
                **resolved, strix_runs="", operator_hints_dir="", host_workspace_dir="", dry_run=False,
            )
        if settings.dry_run or settings.validate() or settings_store.is_masked(settings.llm_api_key):
            raise ValueError("invalid model configuration")
        normalize_api_base(settings.llm_api_base)
        return settings
    except (OSError, ValueError, TypeError, HTTPException) as exc:
        raise GenerationError("model_unavailable") from exc


async def _synthesize(source: SavedReportState, settings: EngineSettings) -> str | None:
    # Override only the in-memory snapshot: token budgeting must use the model
    # actually selected now, while the original scan configuration stays intact.
    source.run_record["scan_config"]["model"] = settings.strix_llm
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        model = make_platform_model(settings, http_client=client)
        try:
            return await synthesize_executive_report(source, lambda: model)
        finally:
            try:
                await model.close()
            except Exception:
                logger.warning("Could not close report model client for %s", source.run_dir.name)


def _publish(run_dir: Path, report: str, source: SavedReportState, generation: dict) -> None:
    # Re-read at commit time. Never save the snapshot or replace scan state with
    # the current model route; another Console operation may have added metadata.
    record = _json(run_dir, "run.json")
    if not isinstance(record, dict):
        raise GenerationError("storage_error")
    old_report = None
    with contextlib.suppress(FileNotFoundError):
        old_report = _read(run_dir, REPORT)
    record["report_synthesized"] = True
    record["report_generated_at"] = _now()
    record["report_generation_id"] = generation["job_id"]
    if isinstance(source.run_record.get("report_synthesis"), dict):
        record["report_synthesis"] = source.run_record["report_synthesis"]
    artifacts.write_executive_report(run_dir, report)
    try:
        artifacts.write_run_record(run_dir, record)
    except BaseException:
        if old_report is not None:
            artifacts.write_executive_report(run_dir, old_report)
        else:
            (run_dir / REPORT).unlink(missing_ok=True)
        raise


async def _run(run_dir: Path, generation: dict, lease: _Lease) -> None:
    try:
        source = await asyncio.to_thread(load_source, run_dir)
        settings = model_settings(run_dir, source)
        generation.update(model=settings.strix_llm, updated_at=_now())
        _write_status(run_dir, generation)
        report = await _synthesize(source, settings)
        if not report:
            detail = source.run_record.get("report_synthesis") or {}
            code = detail.get("code") if isinstance(detail, dict) else "model_error"
            code = code if code in MESSAGES else "model_error"
            generation.update(
                status="failed", code=code, error=MESSAGES[code],
            )
        else:
            _publish(run_dir, report, source, generation)
            generation["status"] = "completed"
    except asyncio.CancelledError:
        generation.update(status="failed", code="interrupted", error=_SAFE_ERRORS["interrupted"])
    except GenerationError as exc:
        generation.update(status="failed", code=exc.code, error=str(exc))
    except OSError:
        generation.update(status="failed", code="storage_error", error=_SAFE_ERRORS["storage_error"])
    except Exception:
        # Provider errors can contain prompts or authorization headers.
        logger.warning("Report generation failed for %s", run_dir.name)
        generation.update(status="failed", code="model_error", error=_SAFE_ERRORS["model_error"])
    finally:
        generation.update(completed_at=_now(), updated_at=_now())
        try:
            _write_status(run_dir, generation)
        except OSError:
            logger.warning("Could not save report generation status for %s", run_dir.name)
        finally:
            lease.close()


def start(run_dir: Path) -> dict[str, Any]:
    try:
        descriptor = _acquire(run_dir)
        if descriptor is None:
            return status(run_dir)
        lease = _Lease(descriptor)
        generation = {
            "status": "running", "job_id": uuid.uuid4().hex,
            "started_at": _now(), "updated_at": _now(),
        }
        try:
            _write_status(run_dir, generation)
            task = asyncio.create_task(_run(run_dir, generation.copy(), lease), name="report-generation")
        except BaseException:
            lease.close()
            raise
        key = str(run_dir.resolve())
        _tasks[key] = task
        def done(finished: asyncio.Task) -> None:
            # An asyncio task cancelled before its first step never enters the
            # coroutine's finally block. The callback owns the same safe lease.
            try:
                if _tasks.get(key) is finished:
                    _tasks.pop(key, None)
                if finished.cancelled():
                    generation.update(
                        status="failed", code="interrupted", error=_SAFE_ERRORS["interrupted"],
                        completed_at=_now(), updated_at=_now(),
                    )
                    with contextlib.suppress(OSError):
                        _write_status(run_dir, generation)
                else:
                    finished.exception()
            finally:
                # Publish cancellation while still owning the lease. Releasing
                # it first lets another Console start a job whose status this
                # callback would then overwrite with the cancelled job's result.
                lease.close()

        task.add_done_callback(done)
        return generation
    except OSError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "storage_error", "message": _SAFE_ERRORS["storage_error"],
        }) from exc


async def shutdown() -> None:
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
