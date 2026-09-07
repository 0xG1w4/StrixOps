"""Evidence delivery contracts using only local, synthetic output files."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from strixops.platform.events import EventWriter
from strixops.report import evidence
from strixops.report.state import RunState


def _state(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workspace = tmp_path / "workspace"
    (workspace / "output").mkdir(parents=True)
    return RunState(run_dir, EventWriter(run_dir)), workspace


def _index(state):
    return json.loads((state.run_dir / "evidence" / ".evidence_index.json").read_text())


def test_large_and_hidden_evidence_are_copied_and_fully_hashed(tmp_path):
    state, workspace = _state(tmp_path)
    output = workspace / "output"
    # Exceeds the old 50 MiB metadata-only threshold without holding a dump in memory.
    large = output / "synthetic-dump.bin"
    with large.open("wb") as stream:
        stream.truncate(50 * 1024 * 1024 + 1)
    (output / ".env").write_bytes(b"LOCAL_FIXTURE=synthetic\n")
    assert state.collect_evidence(workspace) == 2
    entries = {entry["filename"]: entry for entry in _index(state)}
    digest = hashlib.sha256()
    with large.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    assert entries[large.name]["sha256"] == digest.hexdigest()
    assert (state.run_dir / "evidence" / large.name).stat().st_size == large.stat().st_size
    assert entries[".env"]["sha256"] == hashlib.sha256((output / ".env").read_bytes()).hexdigest()
    assert all(
        entry["captured"] and entry["persisted"] and entry["deliverable"] for entry in entries.values()
    )
    record = json.loads((state.run_dir / "run.json").read_text())
    assert record["evidence"]["count"] == record["evidence"]["persisted_count"] == 2
    assert record["evidence"]["status"] == "complete"
    assert large.exists()  # Original workspace content is retained.


def test_copy_error_is_visible_and_not_counted_as_delivered(tmp_path, monkeypatch):
    state, workspace = _state(tmp_path)
    (workspace / "output" / "readable.txt").write_text("synthetic result")
    (workspace / "output" / "denied.txt").write_text("synthetic inaccessible result")
    original = evidence._copy_file

    def deny(directory, name, target, rel):
        if name == "denied.txt":
            raise PermissionError("fixture access denied")
        return original(directory, name, target, rel)

    monkeypatch.setattr(evidence, "_copy_file", deny)
    assert state.collect_evidence(workspace) == 1
    summary = state.run_record["evidence"]
    assert summary["captured_count"] == 2 and summary["persisted_count"] == 1
    assert summary["failed_count"] == 1 and summary["status"] == "incomplete"
    assert summary["files"] == ["readable.txt"]
    entry = next(item for item in _index(state) if item["filename"] == "denied.txt")
    assert entry["captured"] is True
    assert entry["persisted"] is entry["deliverable"] is False
    assert entry["error"] == "fixture access denied"
    assert (workspace / "output" / "denied.txt").exists()


def test_workspace_symlinks_and_special_files_never_escape_archive(tmp_path):
    state, workspace = _state(tmp_path)
    outside = tmp_path / "private"
    outside.mkdir()
    secret = outside / "private.txt"
    secret.write_text("synthetic host-private fixture")
    (workspace / "output" / "outside.txt").symlink_to(secret)
    (workspace / "output" / "outside-dir").symlink_to(outside, target_is_directory=True)
    os.mkfifo(workspace / "output" / "pipe")
    assert state.collect_evidence(workspace) == 0
    assert state.run_record["evidence"]["status"] == "incomplete"
    assert {entry["filename"] for entry in _index(state)} == {"outside.txt", "outside-dir", "pipe"}
    assert all(not entry["deliverable"] for entry in _index(state))
    assert not (state.run_dir / "evidence" / "outside.txt").exists()
    assert not (state.run_dir / "evidence" / "outside-dir").exists()


def test_destination_symlink_is_rejected_without_overwriting_external_file(tmp_path):
    state, workspace = _state(tmp_path)
    (workspace / "output" / "sample.txt").write_text("new result")
    archive = state.run_dir / "evidence"
    archive.mkdir()
    outside = tmp_path / "untouched.txt"
    outside.write_text("original")
    (archive / "sample.txt").symlink_to(outside)
    assert state.collect_evidence(workspace) == 0
    assert outside.read_text() == "original"
    assert "symlink" in _index(state)[0]["error"]


def test_incomplete_copy_does_not_replace_existing_attachment(tmp_path, monkeypatch):
    state, workspace = _state(tmp_path)
    (workspace / "output" / "sample.txt").write_text("new result")
    archive = state.run_dir / "evidence"
    archive.mkdir()
    (archive / "sample.txt").write_text("previous result")

    def fail_fsync(fd):
        raise OSError("fixture full disk")

    monkeypatch.setattr(evidence.os, "fsync", fail_fsync)
    assert state.collect_evidence(workspace) == 0
    assert (archive / "sample.txt").read_text() == "previous result"
    assert not list(archive.glob(".evidence-*.tmp"))
    assert _index(state)[0]["deliverable"] is False


def test_finding_references_are_validated_and_visible_in_report(tmp_path):
    state, workspace = _state(tmp_path)
    (workspace / "output" / "observed.txt").write_text("verified fixture")
    state.set_scan_config({"target": "fixture.invalid", "scan_type": "internal", "report_language": "en"})
    state.add_internal_finding(
        {
            "title": "Synthetic observation",
            "finding_type": "architecture",
            "content": "See /workspace/output/observed.txt and /workspace/output/absent.txt.",
            "metadata": {"evidence_files": ["observed.txt", "../private.txt"]},
        },
        agent_id="root",
        agent_name="root",
    )
    state.collect_evidence(workspace)
    summary = state.run_record["evidence"]
    assert summary["missing_reference_count"] == 2
    assert summary["status"] == "incomplete"
    assert all(ref["deliverable"] for ref in summary["references"] if ref["filename"] == "observed.txt")
    state.update_final_fields(
        executive_summary="Summary",
        methodology="Synthetic fixture",
        technical_analysis="Observed",
        recommendations="Review",
    )
    state.write_executive_report()
    report = (state.run_dir / "penetration_test_report.md").read_text()
    assert "Captured: 1; persisted: 1; deliverable: 1. Status: incomplete." in report
    assert "absent.txt" in report and "missing_reference" in report


def test_empty_workspace_still_has_a_valid_delivery_manifest(tmp_path):
    state, workspace = _state(tmp_path)
    (workspace / "output").rmdir()
    assert state.collect_evidence(workspace) == 0
    assert _index(state) == []
    assert state.run_record["evidence"]["status"] == "complete"
