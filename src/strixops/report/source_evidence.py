"""Read referenced, delivered text evidence for the report model.

The attachment inventory is the only authority for names. Never follow a path
from finding prose directly, and never treat an excerpt as an entire artifact.
"""

from __future__ import annotations

import heapq
import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from strixops.report.evidence import finding_references

if TYPE_CHECKING:
    from strixops.report.state import RunState

_FILE_LIMIT = 80
_FILE_READ_LIMIT = 4 * 1024 * 1024
_TOTAL_READ_LIMIT = 32 * 1024 * 1024
_ATOMIC_SUFFIXES = {
    ".py",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".js",
    ".ts",
    ".rb",
    ".pl",
    ".php",
    ".c",
    ".cpp",
    ".go",
    ".rs",
    ".java",
    ".sql",
    ".json",
    ".jsonl",
    ".ndjson",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".csv",
    ".pem",
    ".key",
}
_BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".7z",
    ".tar",
    ".pcap",
    ".pcapng",
    ".sqlite",
    ".db",
    ".exe",
    ".bin",
    ".woff",
}
_TERM = re.compile(r"[\w./:@-]{4,}")
_COMMON_TERMS = {
    "evidence",
    "output",
    "workspace",
    "finding",
    "source",
    "content",
    "technical_analysis",
    "description",
    "vulnerability",
    "request",
    "response",
    "http",
    "https",
    "confirmed",
}


def _strings(value: Any, depth: int = 0) -> Iterator[str]:
    if depth > 24:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item, depth + 1)


def _reference_map(
    run_state: RunState, inventory: dict[str, dict[str, str]]
) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    evidence = run_state.run_record.get("evidence")
    references = evidence.get("references") if isinstance(evidence, dict) else None
    references = [row for row in references if isinstance(row, dict)] if isinstance(references, list) else []
    by_name = {
        name: dict(row, deliverable=True, captured=True, persisted=True) for name, row in inventory.items()
    }
    # A failed exact archive path must not resolve to another saved file via
    # the workspace-relative output/ alias. Recorded delivery references know
    # about these failed entries even though the attachment inventory omits them.
    for reference in references:
        name = reference.get("filename")
        if isinstance(name, str) and name not in by_name:
            path = PurePosixPath(name)
            if name and not path.is_absolute() and ".." not in path.parts and path != PurePosixPath("."):
                by_name[name] = {"filename": name, "deliverable": False}
    index = list(by_name.values())
    selected: dict[str, set[str]] = {}
    terms: dict[str, list[str]] = {}
    findings = [row for row in [*run_state.reports, *run_state.internal_findings] if isinstance(row, dict)]
    if isinstance(run_state.final_fields, dict):
        findings.append(dict(run_state.final_fields, id="root-draft"))

    def add_reference(reference: dict[str, Any], fid: str) -> None:
        names = reference.get("files") if reference.get("reference_type") == "directory" else None
        if not isinstance(names, list):
            names = [reference.get("filename")]
        for name in names:
            if isinstance(name, str) and name in inventory:
                selected.setdefault(name, set()).add(fid)

    for finding in findings:
        fid = str(finding.get("id") or "unassigned")
        # The shared resolver expects dict metadata. Old or malformed state must
        # not turn optional evidence enrichment into a failed report.
        metadata = finding.get("metadata")
        values = list(_strings(finding))
        normalized = {
            "id": fid,
            "content": "\n".join(values),
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
        for reference in finding_references([normalized], index):
            add_reference(reference, fid)
        preferred = [finding.get(key) for key in ("endpoint", "host", "target", "title", "cwe")]
        candidates: dict[str, None] = {}
        for value in [*_strings(preferred), *values]:
            for match in _TERM.finditer(value.casefold()):
                word = match.group()
                if (
                    word not in _COMMON_TERMS
                    and len(word) <= 128
                    and not word.startswith("/workspace/output/")
                ):
                    candidates[word] = None
                if len(candidates) >= 48:
                    break
            if len(candidates) >= 48:
                break
        terms[fid] = list(candidates)

    for reference in references:
        add_reference(reference, str(reference.get("finding_id") or "unassigned"))
    return selected, terms


def referenced_evidence_names(run_state: RunState, files: list[dict[str, str]]) -> list[str]:
    """Prioritize finding-linked files before applying an inventory size limit."""
    selected, _ = _reference_map(run_state, {row["filename"]: row for row in files})
    return list(selected)


def _open_directory(path: Path) -> int:
    """Anchor every component, including the run directory, without symlinks."""
    absolute = path.absolute()
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            if part in {".", ".."}:
                raise OSError("unsafe directory component")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_text(root_fd: int, filename: str, remaining: int) -> tuple[str | None, int, str | None]:
    """Read one bounded regular file through no-follow directory descriptors."""
    parent_fd = os.dup(root_fd)
    file_fd: int | None = None
    read_bytes = 0
    try:
        parts = PurePosixPath(filename).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            return None, 0, "not_regular_file"
        if before.st_size > _FILE_READ_LIMIT:
            return None, 0, "file_read_limit"
        if before.st_size > remaining:
            return None, 0, "total_read_limit"
        chunks = []
        # Read one sentinel byte only when there is room inside both I/O caps.
        # Size/mtime checks detect growth when no sentinel fits.
        bound = min(before.st_size + 1, _FILE_READ_LIMIT, remaining)
        while read_bytes < bound:
            chunk = os.read(file_fd, min(64 * 1024, bound - read_bytes))
            if not chunk:
                break
            chunks.append(chunk)
            read_bytes += len(chunk)
        after = os.fstat(file_fd)
        if read_bytes != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            return None, read_bytes, "changed_during_read"
        raw = b"".join(chunks)
        if any(byte < 32 and byte not in {9, 10, 13, 27} for byte in raw):
            return None, read_bytes, "binary_content"
        try:
            return raw.decode("utf-8"), read_bytes, None
        except UnicodeDecodeError:
            return None, read_bytes, "non_utf8_content"
    except OSError:
        return None, read_bytes, "unreadable_or_unsafe_path"
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)


def _units(lines: list[str]) -> list[tuple[int, int]]:
    """Whole lines for logs, whole fenced code blocks for Markdown evidence."""
    units = []
    start = 0
    while start < len(lines):
        fence = re.match(r"^\s*(`{3,}|~{3,})", lines[start])
        pem = re.match(r"^-----BEGIN ([A-Z ]+)-----", lines[start])
        end = start + 1
        if fence:
            marker = fence.group(1)
            while end < len(lines):
                closing = re.match(r"^\s*([`~]+)\s*$", lines[end])
                end += 1
                if closing and closing.group(1)[0] == marker[0] and len(closing.group(1)) >= len(marker):
                    break
        elif pem:
            while end < len(lines):
                closing = lines[end].strip() == f"-----END {pem.group(1)}-----"
                end += 1
                if closing:
                    break
        units.append((start, end))
        start = end
    return units


def _excerpt(
    base: dict[str, Any], content: str, terms: list[str], max_tokens: int, count: Callable[[str], int]
) -> dict[str, Any] | None:
    lines = content.splitlines(keepends=True)
    units = _units(lines)
    scored = []
    for unit_index, (start, end) in enumerate(units):
        lowered = "".join(lines[start:end]).casefold()
        score = sum(1 for term in terms if term in lowered)
        if score:
            scored.append((score, start, end, unit_index))
    # Never substitute an arbitrary prefix for relevant evidence.
    if not scored:
        return None
    selected: list[tuple[int, int]] = []

    def block(ranges: list[tuple[int, int]]) -> dict[str, Any]:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return {
            **base,
            "mode": "excerpt",
            "source_line_count": len(lines),
            "omitted_line_count": len(lines) - sum(end - start for start, end in merged),
            "excerpts": [
                {"start_line": start + 1, "end_line": end, "content": "".join(lines[start:end])}
                for start, end in merged
            ],
        }

    result = None
    for _, start, end, unit_index in heapq.nlargest(96, scored, key=lambda item: (item[0], -item[1])):
        # Keep nearby status/error lines with their request whenever possible;
        # adjacent fenced code and multiline keys remain indivisible units.
        context = (units[max(0, unit_index - 2)][0], units[min(len(units) - 1, unit_index + 2)][1])
        for proposed in dict.fromkeys([context, (start, end)]):
            candidate = block([*selected, proposed])
            if count(json.dumps(candidate, ensure_ascii=False, indent=2)) <= max_tokens:
                selected.append(proposed)
                result = candidate
                break
    return result


@dataclass
class _Candidate:
    base: dict[str, Any]
    content: str
    terms: list[str]
    atomic: bool

    def fit(self, limit: int, count: Callable[[str], int]) -> tuple[dict[str, Any] | None, str]:
        if limit <= 0:
            return None, "input_token_budget"
        full = {**self.base, "mode": "full", "content": self.content}
        if count(json.dumps(full, ensure_ascii=False, indent=2)) <= limit:
            return full, ""
        if self.atomic:
            return None, "atomic_document_exceeds_token_budget"
        excerpt = _excerpt(self.base, self.content, self.terms, limit, count)
        if excerpt is not None:
            return excerpt, "excerpted"
        return None, "no_complete_relevant_unit_fits_token_budget"


def _atomic_document(filename: str, content: str) -> bool:
    stripped = content.lstrip()
    json_array = False
    if stripped.startswith("["):
        with suppress(ValueError, RecursionError):
            json_array = isinstance(json.loads(content), list)
    return bool(
        PurePosixPath(filename).suffix.casefold() in _ATOMIC_SUFFIXES
        or stripped.startswith(("#!", "{", "-----BEGIN "))
        or json_array
        or re.search(r"(?:^|[_.-])(?:poc|exploit|script)(?:[_.-]|$)", filename.casefold())
    )


def _balance(
    candidates: list[_Candidate],
    choices: list[tuple[dict[str, Any] | None, str]],
    total_budget: int,
    block_limit: int,
    count: Callable[[str], int],
) -> list[tuple[dict[str, Any] | None, str]]:
    """Share a constrained budget without reading any attachment again."""

    def charge(block: dict[str, Any]) -> int:
        return count(json.dumps(block, ensure_ascii=False, indent=2)) + 16

    def used(selected: list[tuple[dict[str, Any] | None, str]]) -> int:
        blocks = [block for block, _ in selected if block is not None]
        if not blocks:
            return 0
        # Also account for indentation when these blocks become one JSON array.
        return max(
            sum(charge(block) for block in blocks),
            count(json.dumps(blocks, ensure_ascii=False, indent=2)) + 16,
        )

    if used(choices) <= total_budget:
        return choices
    eligible = [index for index, (block, _) in enumerate(choices) if block is not None]
    selected = [(None, reason or "input_token_budget") for _, reason in choices]
    share = max(0, total_budget) // max(1, len(eligible))

    def fit_remaining(index: int, limit: int) -> list[tuple[dict[str, Any] | None, str]] | None:
        # Array indentation/token boundaries are not exactly additive. Correct
        # the allocation before dropping a later file's entire representation.
        for _ in range(3):
            proposal = selected.copy()
            proposal[index] = candidates[index].fit(min(block_limit, limit), count)
            overflow = used(proposal) - total_budget
            if overflow <= 0:
                return proposal
            limit -= overflow + 16
        return None

    # Reserving a share for each file prevents an early verbose log from taking
    # all evidence capacity before a later finding gets even one complete line.
    for index in eligible:
        proposal = fit_remaining(index, share - 16)
        if proposal is not None:
            selected = proposal
    # Reuse room left by small files or indivisible units. Prefer files that
    # have no representation, then the least represented files, before growth.
    ordered = sorted(eligible, key=lambda index: charge(selected[index][0]) if selected[index][0] else 0)
    for index in ordered:
        previous = selected[index]
        selected[index] = (None, "input_token_budget")
        allowance = total_budget - used(selected)
        proposal = fit_remaining(index, allowance - 16)
        if proposal is not None and proposal[index][0] is not None:
            selected = proposal
        else:
            selected[index] = previous
    return selected


def evidence_blocks(
    run_state: RunState,
    allowed_files: list[dict[str, str]],
    *,
    max_block_tokens: int,
    token_count: Callable[[str], int],
    total_token_budget: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return complete evidence or labeled excerpts, plus explicit omissions.

    Every block fits ``max_block_tokens`` when serialized as indented JSON.
    When supplied, ``total_token_budget`` fairly bounds the complete evidence
    array as well. Read text is retained only inside this call, under the total
    byte limit, so rebalancing never reopens an archive or changes literal data.
    """
    inventory = {}
    for row in allowed_files:
        if not isinstance(row, dict) or not isinstance(row.get("filename"), str):
            continue
        name = row["filename"]
        path = PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
            continue
        if "\x00" in name or path.as_posix() != name or not isinstance(row.get("link"), str):
            continue
        inventory[name] = {"filename": name, "link": row["link"]}
    selected, terms = _reference_map(run_state, inventory)
    if not selected:
        return [], []
    candidates: list[_Candidate] = []
    omissions = []
    root_fd: int | None = None
    remaining = _TOTAL_READ_LIMIT
    try:
        with suppress(OSError):
            root_fd = _open_directory(run_state.run_dir / "evidence")
        for index, (name, finding_ids) in enumerate(selected.items()):
            base = {**inventory[name], "finding_ids": sorted(finding_ids)}
            reason = None
            if index >= _FILE_LIMIT:
                reason = "file_count_limit"
            elif root_fd is None:
                reason = "unreadable_or_unsafe_archive"
            elif PurePosixPath(name).suffix.casefold() in _BINARY_SUFFIXES:
                reason = "binary_file_type"
            else:
                content, consumed, reason = _read_text(root_fd, name, remaining)
                remaining -= consumed
                if reason is None and content is not None:
                    relevant_terms = list(
                        dict.fromkeys(term for fid in finding_ids for term in terms.get(fid, []))
                    )
                    candidates.append(
                        _Candidate(base, content, relevant_terms, _atomic_document(name, content))
                    )
                    continue
            omissions.append({**base, "reason": reason})
    finally:
        if root_fd is not None:
            os.close(root_fd)
    choices = [candidate.fit(max_block_tokens, token_count) for candidate in candidates]
    if total_token_budget is not None:
        choices = _balance(candidates, choices, max(0, total_token_budget), max_block_tokens, token_count)
    blocks = []
    for candidate, (block, reason) in zip(candidates, choices, strict=True):
        if block is not None:
            blocks.append(block)
        if reason:
            omission = {**candidate.base, "reason": reason}
            if block is not None and block["mode"] == "excerpt":
                omission["omitted_line_count"] = block["omitted_line_count"]
            omissions.append(omission)
    return blocks, omissions
