"""Ordered target scope shared by the CLI, Console and native scan engine."""

from __future__ import annotations

from pathlib import Path

MAX_TARGETS = 100
MAX_TARGET_LENGTH = 2048
MAX_TARGET_LIST_BYTES = 1024 * 1024


def normalize_targets(target: str = "", targets: list[str] | None = None) -> list[str]:
    """Resolve the legacy primary target and an optional complete scope.

    Exact duplicates are removed after trimming. URLs are deliberately not
    canonicalized: different paths, schemes and query strings can be distinct
    operator-approved targets.
    """
    if not isinstance(target, str):
        raise ValueError("target must be a string")
    if targets is not None and not isinstance(targets, list):
        raise ValueError("targets must be a list of strings")
    primary = target.strip()
    values = targets if targets else [target]
    result: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values, 1):
        if not isinstance(value, str):
            raise ValueError(f"target {index} must be a string")
        value = value.strip()
        if not value:
            raise ValueError(f"target {index} is empty")
        if len(value) > MAX_TARGET_LENGTH:
            raise ValueError(f"target {index} exceeds {MAX_TARGET_LENGTH} characters")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"target {index} contains a control character")
        if value not in seen:
            seen.add(value)
            result.append(value)
            if len(result) > MAX_TARGETS:
                raise ValueError(f"at most {MAX_TARGETS} unique targets are allowed")
    if primary and primary != result[0]:
        raise ValueError("target must match the first entry in targets")
    return result


def read_target_list(path: str | Path) -> list[str]:
    """Read a bounded UTF-8 list, ignoring blank lines and full-line comments."""
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"target list is not a readable file: {source}")
    try:
        with source.open("rb") as handle:
            payload = handle.read(MAX_TARGET_LIST_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"cannot read target list {source}: {exc.strerror}") from exc
    if len(payload) > MAX_TARGET_LIST_BYTES:
        raise ValueError(f"target list exceeds {MAX_TARGET_LIST_BYTES} bytes: {source}")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"target list must use UTF-8: {source}") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return normalize_targets(targets=lines) if lines else []
