"""Persist workspace evidence without size limits or following workspace symlinks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from strixops.platform import artifacts

# Absolute workspace references in prose are checked as well as the explicit
# metadata.evidence_files list. Prose punctuation delimits a path; structured
# references preserve exact filenames, including spaces and punctuation.
_WORKSPACE_REFERENCE = re.compile(
    r"/workspace/output/[^\s<>\"'`()\[\]{}，。；：、（）【】「」『』《》“”‘’,;:!?！？]*"
)


def evidence_filename(reference: str) -> str | None:
    """Resolve a declared evidence path, rejecting absolute paths and traversal."""
    prefix = "/workspace/output/"
    if reference == prefix:
        return "."
    name = reference[len(prefix) :] if reference.startswith(prefix) else reference
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        return None
    return path.as_posix()


def _reference_delivery(
    name: str | None, reference: str, by_name: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    entry = by_name.get(name, {})
    prefix = "" if name == "." else f"{name}/"
    members = (
        [item for filename, item in sorted(by_name.items()) if filename.startswith(prefix)]
        if name is not None
        else []
    )
    directory = name == "." or reference.endswith("/") or (not entry and bool(members))
    if directory:
        # Only the no-follow collection manifest can establish descendants.
        # A named symlink/special file must never become a directory reference.
        if entry:
            members = []
        delivered = bool(members) and all(item.get("deliverable") for item in members)
        return {
            "reference_type": "directory",
            "file_count": len(members),
            "files": [item["filename"] for item in members],
            "captured": bool(members) and all(item.get("captured") for item in members),
            "persisted": bool(members) and all(item.get("persisted") for item in members),
            "deliverable": delivered,
            **(
                {
                    "error": entry.get("error")
                    or ("incomplete_directory_reference" if members else "missing_reference")
                }
                if not delivered
                else {}
            ),
        }
    return {
        "captured": bool(entry.get("captured")),
        "persisted": bool(entry.get("persisted")),
        "deliverable": bool(entry.get("deliverable")),
        **({"error": entry.get("error", "missing_reference")} if not entry.get("deliverable") else {}),
    }


def finding_references(findings: list[dict[str, Any]], index: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {entry["filename"]: entry for entry in index}
    references = []
    for finding in findings:
        explicit = (finding.get("metadata") or {}).get("evidence_files", [])
        if isinstance(explicit, str):
            explicit = [explicit]
        refs = {ref for ref in explicit if isinstance(ref, str)} if isinstance(explicit, list) else set()
        for field in ("content", "source", "evidence", "technical_analysis"):
            refs.update(
                ref.rstrip(".,;:") for ref in _WORKSPACE_REFERENCE.findall(str(finding.get(field) or ""))
            )
        for reference in sorted(refs):
            name = evidence_filename(reference)
            references.append(
                {
                    "finding_id": finding.get("id", ""),
                    "reference": reference,
                    "filename": name,
                    **_reference_delivery(name, reference, by_name),
                }
            )
    return references


def _destination(target: Path, rel: str) -> Path:
    """Evidence archive directories must never follow a pre-existing symlink."""
    dest = target / rel
    current = target
    for part in Path(rel).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise OSError("evidence destination contains a symlink")
        current.mkdir(exist_ok=True, mode=0o700)
    if dest.is_symlink():
        raise OSError("evidence destination is a symlink")
    return dest


def _copy_file(source_fd: int, name: str, target: Path, rel: str) -> tuple[int, str]:
    """Copy and hash a regular file through anchored, no-follow descriptors.

    Stream in bounded chunks, publish only after fsync, and reject evidence
    modified during copying. No partial destination is presented as delivered.
    """
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd)
    temporary: Path | None = None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("evidence must be a regular file")
        dest = _destination(target, rel)
        digest = hashlib.sha256()
        copied = 0
        with (
            os.fdopen(fd, "rb", closefd=False) as source,
            tempfile.NamedTemporaryFile(
                dir=dest.parent, prefix=".evidence-", suffix=".tmp", delete=False
            ) as output,
        ):
            temporary = Path(output.name)
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                output.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(fd)
        if copied != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OSError("evidence changed during collection; source retained for recovery")
        temporary.replace(dest)
        temporary = None
        return copied, digest.hexdigest()
    finally:
        os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def collect_files(workspace: Path, target: Path, category_for: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Return every observed file's delivery status and traversal errors.

    The caller retains the host workspace even when collection fails. Symlinks
    and special files are indexed as undeliverable, never dereferenced.
    """
    index: list[dict[str, Any]] = []
    errors: list[str] = []
    source = workspace / "output"
    if target.is_symlink():
        return [], ["evidence archive is a symlink"]
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            errors.append(f"Cannot open workspace/output: {exc}")
        artifacts.atomic_write_text(target / ".evidence_index.json", "[]")
        return index, errors
    try:
        for root, dirs, files, directory_fd in os.fwalk(
            ".", dir_fd=source_fd, follow_symlinks=False, onerror=lambda exc: errors.append(str(exc))
        ):
            # fwalk does not follow symlink directories. Include them in the
            # manifest so a claimed artifact cannot disappear silently.
            links = [
                name
                for name in dirs
                if stat.S_ISLNK(os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode)
            ]
            for name in sorted([*files, *links]):
                rel = (Path(root) / name).as_posix()
                if rel == ".evidence_index.json":
                    # Reserved manifest name: never overwrite our generated index.
                    errors.append("workspace/output/.evidence_index.json is reserved; source retained")
                    continue
                entry: dict[str, Any] = {
                    "filename": rel,
                    "size": 0,
                    "category": category_for(rel),
                    "collected_at": artifacts.utc_stamp(),
                    "captured": False,
                    "persisted": False,
                    "deliverable": False,
                }
                try:
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    entry["size"] = info.st_size
                    if not stat.S_ISREG(info.st_mode):
                        raise OSError("symlinks and special files are not evidence attachments")
                    entry["captured"] = True
                    size, digest = _copy_file(directory_fd, name, target, rel)
                    entry.update(
                        size=size,
                        sha256=digest,
                        persisted=True,
                        deliverable=True,
                        archive_path=f"evidence/{rel}",
                    )
                except OSError as exc:
                    entry["error"] = str(exc)
                index.append(entry)
    except OSError as exc:
        errors.append(str(exc))
    finally:
        os.close(source_fd)
    index.sort(key=lambda entry: entry["filename"])
    artifacts.atomic_write_text(
        target / ".evidence_index.json", json.dumps(index, ensure_ascii=False, indent=2)
    )
    return index, errors
