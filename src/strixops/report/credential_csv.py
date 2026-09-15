"""Import only declared/specially named credential CSVs from the saved manifest.

The CSV is converted to inventory rows; raw attachment text never enters the
report. Scripts, logs and unrelated evidence files are not opened.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from strixops.report.credentials import _key, collect_credentials, merge_credentials
from strixops.report.evidence import finding_references

_FILE_LIMIT = 4 * 1024 * 1024
_TOTAL_LIMIT = 16 * 1024 * 1024
_FILE_COUNT = 100
_ROW_LIMIT = 20000
_NAME = re.compile(
    r"(?:^|[_.-])(?:credentials?|creds?|passwords?|secrets?|hashes?|tokens?|api[_-]?keys?)(?:[_.-]|$)", re.I
)


class _CSVLimitError(ValueError):
    pass


def imported_csv_paths(run_dir: Path, datasets: list[dict], *, store: Any = None) -> set[str]:
    """Skip a captured CSV only when its digest matches a committed full import.

    This reads the small engine-created capture index, never the dump body.
    A changed CSV captured after an import remains eligible for the legacy reader.
    """
    if not datasets:
        return set()
    try:
        raw = _read(run_dir, ".evidence_index.json", _FILE_LIMIT)
        index = json.loads(raw)
        if not isinstance(index, list):
            return set()
        if store is not None:
            matched = store.get_datasets(paths=(
                "/workspace/output/" + row["filename"] for row in index
                if isinstance(row, dict) and isinstance(row.get("filename"), str)
                and row["filename"].lower().endswith(".csv")
            ))
            if matched.get("success"):
                datasets = matched["datasets"]
        imported = {
            (row.get("path", "").removeprefix("/workspace/output/"), row.get("sha256"), row.get("size"))
            for row in datasets
            if isinstance(row, dict) and row.get("sha256") and isinstance(row.get("path"), str)
        }
        return {
            row["filename"] for row in index
            if isinstance(row, dict) and row.get("deliverable") is True
            and isinstance(row.get("filename"), str)
            and isinstance(row.get("sha256"), str) and type(row.get("size")) is int
            and (row["filename"], row.get("sha256"), row.get("size")) in imported
        }
    except (OSError, ValueError, UnicodeError, RecursionError):
        return set()


def _read(run_dir: Path, filename: str, limit: int) -> str:
    directory = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in ("evidence", *PurePosixPath(filename).parts[:-1]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            PurePosixPath(filename).name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("unsafe credential CSV")
            if before.st_size > limit:
                raise _CSVLimitError("oversized credential CSV")
            raw = source.read(limit + 1)
            after = os.fstat(source.fileno())
        if len(raw) > limit:
            raise _CSVLimitError("oversized credential CSV")
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("credential CSV changed or exceeded limit")
        return raw.decode("utf-8-sig")
    finally:
        os.close(directory)


def load_credential_csv(
    *,
    run_dir: Path,
    manifest: dict | None,
    reports: list[dict],
    internal_findings: list[dict],
    skip_paths: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Import complete rows only; report invalid/oversized candidate files explicitly."""
    warnings: list[str] = []
    if not manifest:
        return [], warnings
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files", []), list):
        return [], ["credential_csv_unreadable"]
    names: dict[str, None] = {}
    for value in manifest.get("files", []):
        if not isinstance(value, str):
            warnings.append("credential_csv_unreadable")
            continue
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\x00" in value:
            warnings.append("credential_csv_unreadable")
            continue
        if path.suffix.casefold() == ".csv":
            names[path.as_posix()] = None
    index = [{"filename": name, "deliverable": True, "captured": True, "persisted": True} for name in names]
    linked: dict[str, list[dict]] = {}
    for finding in [*reports, *internal_findings]:
        if not isinstance(finding, dict):
            continue
        metadata = finding.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        is_credential = finding.get("finding_type") == "credential" or metadata.get("credential_csv") is True
        if not is_credential:
            continue
        safe_finding = {**finding, "metadata": metadata}
        for reference in finding_references([safe_finding], index):
            # An entire referenced folder is not an explicit credential CSV.
            name = reference.get("filename")
            if reference.get("reference_type") != "directory" and isinstance(name, str) and name in names:
                linked.setdefault(name, []).append(
                    {
                        "kind": "finding",
                        "id": str(finding.get("id") or ""),
                        "title": str(finding.get("title") or finding.get("id") or ""),
                    }
                )
    selected = [
        name for name in names if name not in (skip_paths or set())
        and (name in linked or _NAME.search(PurePosixPath(name).name))
    ]
    if len(selected) > _FILE_COUNT:
        warnings.append("credential_csv_limit")
        selected = selected[:_FILE_COUNT]
    used = 0
    result: list[dict] = []
    for name in selected:
        if used >= _TOTAL_LIMIT or len(result) >= _ROW_LIMIT:
            warnings.append("credential_csv_limit")
            break
        try:
            text = _read(run_dir, name, min(_FILE_LIMIT, _TOTAL_LIMIT - used))
            used += len(text.encode("utf-8"))
            reader = csv.reader(io.StringIO(text, newline=""), strict=True)
            header = next(reader, [])
            keys = [_key(key) for key in header]
            if not any(
                key in {"password", "hash", "api_key", "token", "secret", "private_key", "encryption_key"}
                for key in keys
            ):
                raise ValueError("not a credential CSV header")
            if len([key for key in keys if key]) != len({key for key in keys if key}):
                raise ValueError("duplicate credential CSV fields")
            raw_rows = []
            for values in reader:
                if not values:
                    continue
                if len(values) != len(keys):
                    raise ValueError("invalid credential CSV row")
                if len(raw_rows) + len(result) >= _ROW_LIMIT:
                    raise _CSVLimitError("credential row limit")
                raw_rows.append({key: value for key, value in zip(keys, values, strict=True) if key})
            imported = collect_credentials(
                reports=[{"id": f"evidence/{name}", "metadata": {"credentials": raw_rows}}],
                internal_findings=[],
                notes=[],
                assessment={},
                final_fields={},
            )
            for row in imported:
                row["sources"] = [
                    {"kind": "credential_csv", "id": f"evidence/{name}", "title": name},
                    *linked.get(name, []),
                ]
            result.extend(imported)
        except _CSVLimitError:
            warnings.append("credential_csv_limit")
        except (OSError, UnicodeError, ValueError, csv.Error, RecursionError):
            warnings.append("credential_csv_unreadable")
    return merge_credentials(result), list(dict.fromkeys(warnings))
