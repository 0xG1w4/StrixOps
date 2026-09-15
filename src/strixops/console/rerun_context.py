"""Validated, immutable final-report snapshots for a new assessment."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

REPORT = "penetration_test_report.md"
SNAPSHOT = "previous_report.md"
MAX_REPORT_BYTES = 8 * 1024 * 1024
TERMINAL = {"completed", "failed", "interrupted", "cancelled", "aborted", "stopped"}
MESSAGES = {
    "task_active": "The task or its finalization is still running. Wait until it has ended.",
    "report_not_final": "This task has no confirmed final model report. A saved draft cannot be used.",
    "report_missing": "The final report file is missing. Generate a report first.",
    "report_empty": "The saved final report is empty. Generate the report again.",
    "report_unreadable": "The final report or its saved metadata cannot be read safely.",
    "report_changing": "The final report is being published. Refresh the report and try again.",
    "report_changed": "The final report changed after preview. Refresh it before continuing.",
    "report_too_large": "The complete report exceeds the snapshot size limit. Use a new task.",
    "context_budget_exceeded": (
        "The full report and task instructions exceed the selected model's input budget. "
        "Choose a model with a larger context or use a new task. The report was not truncated."
    ),
    "invalid_continuation": "Select a source task and its current final report before continuing.",
}


class ContinuationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(MESSAGES[code])


@contextmanager
def publication_lock(run_dir: Path, *, reading: bool = False):
    """Lock only the report/metadata commit, never the model generation job.

    Readers fail promptly while publication is in progress. The publisher may
    wait for an in-flight bounded snapshot read, then commit both files.
    """
    directory = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        descriptor = os.open(
            ".report-publication.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600, dir_fd=directory,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("publication lock is not a regular file")
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH | fcntl.LOCK_NB) if reading else fcntl.LOCK_EX)
        except BlockingIOError as exc:
            raise ContinuationError("report_changing") from exc
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def read_snapshot(run_dir: Path, *, engine_running: bool = False) -> dict:
    # Imported lazily: report_generation also uses the short publication lock.
    from strixops.console.report_generation import _read

    try:
        with publication_lock(run_dir, reading=True):
            record = json.loads(_read(run_dir, "run.json", limit=32 * 1024 * 1024))
            if not isinstance(record, dict):
                raise ValueError("invalid run record")
            cleanup = record.get("cleanup") or {}
            if (
                engine_running or record.get("status") not in TERMINAL
                or not isinstance(cleanup, dict) or cleanup.get("status") == "in_progress"
            ):
                raise ContinuationError("task_active")
            if record.get("report_synthesized") is not True:
                raise ContinuationError("report_not_final")
            try:
                # Read one byte beyond our limit to distinguish size from encoding errors.
                markdown = _read(run_dir, REPORT, limit=MAX_REPORT_BYTES + 1)
            except FileNotFoundError as exc:
                raise ContinuationError("report_missing") from exc
            except ValueError as exc:
                if str(exc) == "source exceeds read limit":
                    raise ContinuationError("report_too_large") from exc
                raise
            data = markdown.encode("utf-8")
            if len(data) > MAX_REPORT_BYTES:
                raise ContinuationError("report_too_large")
            if not markdown.strip():
                raise ContinuationError("report_empty")
            metadata = {
                "source_run": run_dir.name,
                "report_sha256": hashlib.sha256(data).hexdigest(),
                "snapshot_file": SNAPSHOT,
            }
            generated_at = record.get("report_generated_at") or record.get("end_time")
            if isinstance(generated_at, str) and generated_at:
                metadata["report_generated_at"] = generated_at
            return {"metadata": metadata, "markdown": markdown}
    except ContinuationError:
        raise
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise ContinuationError("report_unreadable") from exc


def materialize(run_dir: Path, snapshot: dict) -> dict:
    """Persist a validated queue/API snapshot without consulting the old run."""
    if not isinstance(snapshot, dict):
        raise ContinuationError("invalid_continuation")
    metadata, markdown = snapshot.get("metadata"), snapshot.get("markdown")
    if not isinstance(metadata, dict) or not isinstance(markdown, str) or not markdown.strip():
        raise ContinuationError("invalid_continuation")
    source = metadata.get("source_run")
    digest = metadata.get("report_sha256")
    if (
        not isinstance(source, str) or not source or source in {".", ".."}
        or "/" in source or "\\" in source
        or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or hashlib.sha256(markdown.encode("utf-8")).hexdigest() != digest
    ):
        raise ContinuationError("invalid_continuation")
    if len(markdown.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ContinuationError("report_too_large")
    directory = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(SNAPSHOT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(markdown)
    finally:
        os.close(directory)
    result = {"source_run": source, "report_sha256": digest, "snapshot_file": SNAPSHOT}
    if isinstance(metadata.get("report_generated_at"), str):
        result["report_generated_at"] = metadata["report_generated_at"]
    return result
