"""Stream explicitly selected workspace CSVs into the run credential registry."""

from __future__ import annotations

import csv
import errno
import hashlib
import io
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from strixops.report.credential_store import CredentialError, error_result

_FIELDS = {
    "host", "username", "password", "hash", "secret_type", "source", "severity", "note",
    "validation_status", "validation_evidence",
}
# A complete normalized row, including doubled CSV quotes, fits this bound even
# at every field's registry limit. There is deliberately no file/row-count cap.
_MAX_RECORD_CHARS = 256 * 1024


def _relative_parts(csv_path: str) -> list[str]:
    if not isinstance(csv_path, str) or len(csv_path) > 4096 or "\x00" in csv_path:
        raise CredentialError("invalid_arguments")
    try:
        csv_path.encode("utf-8")
    except UnicodeError:
        raise CredentialError("invalid_arguments") from None
    relative = csv_path.removeprefix("/workspace/")
    parts = relative.split("/")
    if (
        len(parts) < 2 or parts[0] != "output"
        or any(part in {"", ".", ".."} for part in parts)
        or not parts[-1].lower().endswith(".csv")
    ):
        raise CredentialError("invalid_arguments")
    return parts


def _identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _fingerprint(info: os.stat_result) -> tuple:
    return (
        *_identity(info), info.st_nlink, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


class _HashingReader(io.RawIOBase):
    def __init__(self, raw: io.FileIO, expected_size: int) -> None:
        self.raw = raw
        self.digest = hashlib.sha256()
        self.size = 0
        self.expected_size = expected_size

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = self.expected_size - self.size
        if remaining <= 0:
            return 0
        # Never chase an actively growing dump; EOF is the original file size
        # and the final fingerprint check rejects any change before commit.
        size = self.raw.readinto(memoryview(buffer)[:remaining])
        if size:
            self.digest.update(memoryview(buffer)[:size])
            self.size += size
        return size


class _CSVLines:
    """Bound physical reads and the CSV parser's current logical record."""

    def __init__(self, stream: io.TextIOWrapper, cancelled: Callable[[], bool]) -> None:
        self.stream = stream
        self.cancelled = cancelled
        self.chars = 0

    def __iter__(self) -> _CSVLines:
        return self

    def __next__(self) -> str:
        if self.cancelled():
            raise CredentialError("cancelled")
        line = self.stream.readline(_MAX_RECORD_CHARS + 1)
        if not line:
            raise StopIteration
        self.chars += len(line)
        if self.chars > _MAX_RECORD_CHARS:
            raise CredentialError("content_limit")
        return line


def _rows(lines: _CSVLines) -> Iterator[dict]:
    reader = csv.reader(lines, strict=True)
    header = next(reader, [])
    if (
        not header or len(header) != len(set(header))
        or not set(header) <= _FIELDS
        or not set(header) & {"username", "password", "hash"}
    ):
        raise CredentialError("invalid_dataset")
    lines.chars = 0
    for values in reader:
        lines.chars = 0
        if not values:
            continue
        if len(values) != len(header):
            raise CredentialError("invalid_dataset")
        row = dict(zip(header, values, strict=True))
        # Empty optional controls use record_credential defaults. Secret values,
        # including an explicitly empty account password, remain byte-for-byte.
        for field in ("secret_type", "validation_status", "source"):
            if row.get(field) == "":
                del row[field]
        yield row


def import_credential_csv(
    *, store: Any, workspace: Path, csv_path: str, source: str,
    agent_id: str, agent_name: str, cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Import every normalized row atomically; errors never expose source values."""
    cancelled = cancelled or (lambda: False)
    try:
        parts = _relative_parts(csv_path)
        if not isinstance(workspace, Path) or not workspace.is_absolute():
            raise CredentialError("storage_unavailable")
        if cancelled():
            raise CredentialError("cancelled")
        dataset_path = "/workspace/" + "/".join(parts)
        metadata: dict[str, Any] = {}
        with ExitStack() as stack:
            directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            root_info = os.fstat(directory)
            ancestors: list[tuple[int, str, tuple]] = []
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                stack.callback(os.close, child)
                ancestors.append((directory, part, _identity(os.fstat(child))))
                directory = child
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
            )
            raw = stack.enter_context(os.fdopen(descriptor, "rb", buffering=0))
            before = os.fstat(raw.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise CredentialError("unsafe_storage")
            hashing = _HashingReader(raw, before.st_size)
            stream = stack.enter_context(
                io.TextIOWrapper(io.BufferedReader(hashing), encoding="utf-8-sig", newline="")
            )

            def rows() -> Iterator[dict]:
                try:
                    yield from _rows(_CSVLines(stream, cancelled))
                    if cancelled():
                        raise CredentialError("cancelled")
                    if (
                        _fingerprint(os.fstat(raw.fileno())) != _fingerprint(before)
                        or _fingerprint(os.stat(parts[-1], dir_fd=directory, follow_symlinks=False))
                        != _fingerprint(before)
                        or _identity(os.stat(workspace, follow_symlinks=False)) != _identity(root_info)
                        or any(
                            _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected
                            for parent, name, expected in ancestors
                        )
                        or hashing.size != before.st_size
                    ):
                        raise CredentialError("storage_unavailable")
                    metadata.update(sha256=hashing.digest.hexdigest(), size=hashing.size)
                except (csv.Error, UnicodeError):
                    raise CredentialError("invalid_dataset") from None
                except OSError:
                    raise CredentialError("storage_unavailable") from None

            return store.import_rows(
                rows(), source=source or dataset_path, agent_id=agent_id, agent_name=agent_name,
                dataset_path=dataset_path, dataset_metadata=metadata, cancelled=cancelled,
            )
    except CredentialError as exc:
        return error_result(exc.code)
    except OSError as exc:
        code = "unsafe_storage" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "storage_unavailable"
        return error_result(code)
    except (UnicodeError, ValueError, TypeError):
        return error_result("invalid_arguments")
