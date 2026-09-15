"""Read-only credential inventory and safe CSV export for saved runs."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import stat
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from strixops.platform.artifacts import csv_safe
from strixops.report.credential_csv import imported_csv_paths, load_credential_csv
from strixops.report.credential_inventory import CredentialInventory
from strixops.report.credential_store import CredentialStore
from strixops.report.credentials import (
    collect_credentials,
)
from strixops.report.notes import NotesError, parse_document

OpenRunFile = Callable[[Path, str], int]
CSV_FIELDS = ("host", "username", "password", "hash", "source", "severity", "note")
_FILE_LIMIT = 4 * 1024 * 1024
_TOTAL_LIMIT = 64 * 1024 * 1024
_FINDING_LIMIT = 2000


class _Reader:
    def __init__(self, run_dir: Path, open_file: OpenRunFile) -> None:
        self.run_dir = run_dir
        self.open_file = open_file
        self.total = 0
        self.available = 0
        self.loaded: set[str] = set()
        self.warnings: list[str] = []

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def text(self, relative: str, code: str, *, limit: int = _FILE_LIMIT) -> str | None:
        try:
            remaining = min(limit, _TOTAL_LIMIT - self.total)
            if remaining <= 0:
                self.warn("source_limit")
                return None
            with os.fdopen(self.open_file(self.run_dir, relative), "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("unsafe credential source")
                raw = source.read(remaining + 1)
            self.total += len(raw)
            if len(raw) > remaining:
                self.warn("source_limit")
                return None
            return raw.decode("utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError):
            self.warn(code)
            return None

    def document(self, relative: str, code: str, expected: type) -> Any:
        text = self.text(relative, code)
        if text is None:
            return expected()
        try:

            def reject(_value: str) -> None:
                raise ValueError("invalid JSON number")

            value = json.loads(text, parse_constant=reject)
            if not isinstance(value, expected):
                raise ValueError("invalid credential source")
            if expected is list and not all(isinstance(row, dict) for row in value):
                raise ValueError("invalid source row")
        except (ValueError, RecursionError):
            self.warn(code)
            return expected()
        self.available += 1
        self.loaded.add(relative)
        return value

    def findings(self, directory: str, code: str) -> list[dict]:
        base = child = None
        try:
            base = os.open(self.run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            child = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
            names = sorted(name for name in os.listdir(child) if name.endswith(".md"))
            self.available += 1
            if len(names) > _FINDING_LIMIT:
                self.warn("source_limit")
                names = names[:_FINDING_LIMIT]
        except FileNotFoundError:
            return []
        except OSError:
            self.warn(code)
            return []
        finally:
            for descriptor in (child, base):
                if descriptor is not None:
                    os.close(descriptor)
        result = []
        for name in names:
            markdown = self.text(f"{directory}/{name}", code)
            if markdown is None:
                continue
            row = {"id": Path(name).stem, "content": markdown}
            header = markdown.split("\n## Details", 1)[0]
            for line in header.splitlines():
                if line.startswith("# "):
                    row.setdefault("title", line[2:].strip())
                match = re.match(r"\*\*(ID|Type|Host|Source|Severity):\*\*\s*(.*)", line)
                if match:
                    row[{"Type": "finding_type"}.get(match[1], match[1].lower())] = match[2]
            metadata = re.search(r"\n## Metadata\s*\n```json\s*\n(.*)\n```\s*$", markdown, re.S)
            if metadata:
                try:
                    row["metadata"] = json.loads(metadata[1])
                except (ValueError, RecursionError):
                    self.warn(code)
            result.append(row)
        return result


def load_inventory(
    run_dir: Path,
    open_file: OpenRunFile,
    *,
    valid_assessment: Callable[[Any], bool] | None = None,
) -> tuple[CredentialInventory, _Reader]:
    """Keep readable sources when another source is corrupt; only import credential CSV attachments."""
    reader = _Reader(run_dir, open_file)
    record = reader.document("run.json", "run_unreadable", dict)
    register = CredentialStore(run_dir)
    metadata = register.report_snapshot(limit=1)
    if metadata.get("success") and metadata.get("source_status") == "available":
        reader.available += 1
    elif not metadata.get("success"):
        reader.warn("credential_register_unreadable")
    reports = reader.document("vulnerabilities.json", "vulnerabilities_unreadable", list)
    # The JSON record is authoritative when its Markdown rendering also exists.
    report_ids = {row.get("id") for row in reports}
    reports.extend(
        row
        for row in reader.findings("vulnerabilities", "vulnerabilities_unreadable")
        if row["id"] not in report_ids
    )
    findings = reader.findings("internal_findings", "findings_unreadable")
    raw_notes = reader.text(".state/notes.json", "notes_unreadable", limit=16 * 1024 * 1024)
    notes: list[dict] = []
    if raw_notes is not None:
        try:
            notes = parse_document(raw_notes.encode("utf-8"))["notes"]
            reader.available += 1
        except NotesError:
            reader.warn("notes_unreadable")
    assessment = reader.document("assessment.json", "assessment_unreadable", dict)
    if "assessment.json" in reader.loaded and valid_assessment and not valid_assessment(assessment):
        reader.warn("assessment_unreadable")
        reader.available -= 1
        assessment = {}
    final_fields = record.get("scan_results")
    if final_fields is not None and not isinstance(final_fields, dict):
        reader.warn("run_unreadable")
        final_fields = None
    campaign = record.get("internal_campaign")
    if campaign is not None and not isinstance(campaign, dict):
        reader.warn("run_unreadable")
        campaign = None
    rows = collect_credentials(
        reports=reports,
        internal_findings=findings,
        notes=notes,
        assessment=assessment,
        final_fields=final_fields,
        campaign=campaign,
    )
    imported, csv_warnings = load_credential_csv(
        run_dir=run_dir,
        manifest=record.get("evidence"),
        reports=reports,
        internal_findings=findings,
        skip_paths=imported_csv_paths(run_dir, metadata.get("datasets", []), store=register),
    )
    inventory = CredentialInventory(
        register, [*rows, *imported], reports=reports, internal_findings=findings,
        datasets=metadata.get("datasets", []),
    )
    for warning in csv_warnings:
        reader.warn(warning)
    return inventory, reader


def source_state(inventory: CredentialInventory, reader: _Reader) -> dict:
    for warning in inventory.warnings:
        reader.warn(warning)
    available = reader.available or inventory.source_status == "available"
    if reader.warnings:
        status = "partial" if available else "unreadable"
    else:
        status = "available" if available else "missing"
    return {"source_status": status, "warnings": reader.warnings}


def list_response(
    run_dir: Path, open_file: OpenRunFile, *,
    valid_assessment: Callable[[Any], bool] | None = None,
    query: str | None = None, validation_status: str | None = None,
    limit: int = 25, offset: int = 0,
) -> dict[str, Any]:
    inventory, reader = load_inventory(run_dir, open_file, valid_assessment=valid_assessment)
    page = inventory.page(query=query, validation_status=validation_status, limit=limit, offset=offset)
    return {**page, **source_state(inventory, reader)}


def csv_chunks(rows: Iterable[dict[str, Any]]) -> Iterator[bytes]:
    """Legacy seven-column layout, BOM and formula-safe quoted literal cells."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    yield output.getvalue().encode("utf-8-sig")
    output.seek(0)
    output.truncate(0)
    for row in rows:
        note = "\n".join(
            filter(
                None,
                [
                    str(row.get("note") or ""),
                    f"validation_evidence={row['validation_evidence']}"
                    if row.get("validation_evidence") else "",
                    f"type={row.get('secret_type', 'secret')}; "
                    f"validation={row.get('validation_status', 'unverified')}",
                ],
            )
        )
        cells = {field: csv_safe(note if field == "note" else row.get(field, "")) for field in CSV_FIELDS}
        writer.writerow(cells)
        if output.tell() >= 64 * 1024:
            yield output.getvalue().encode("utf-8")
            output.seek(0)
            output.truncate(0)
    if output.tell():
        yield output.getvalue().encode("utf-8")


def csv_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    """Convenience for small callers; HTTP exports use bounded chunks directly."""
    return b"".join(csv_chunks(rows))
