"""Import host observations from explicit output files, without running scanners."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from strixops.report.host_inventory import HOST_FIELDS, HostInventory, HostInventoryError

MAX_IMPORT_BYTES = 32 * 1024 * 1024


def _parts(file_path: str) -> list[str]:
    if not isinstance(file_path, str) or len(file_path) > 4096 or "\x00" in file_path:
        raise HostInventoryError("invalid_arguments")
    relative = file_path.removeprefix("/workspace/")
    parts = relative.split("/")
    if (
        len(parts) < 2
        or parts[0] != "output"
        or any(p in {"", ".", ".."} for p in parts)
        or Path(parts[-1]).suffix.lower() not in {".json", ".csv", ".xml"}
    ):
        raise HostInventoryError("invalid_arguments")
    return parts


def _identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _fingerprint(info: os.stat_result) -> tuple:
    return *_identity(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _json_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise HostInventoryError("invalid_dataset")
        result[key] = value
    return result


def _normalized_rows(raw: bytes, extension: str) -> Iterator[dict]:
    decoded = raw.decode("utf-8-sig")
    if extension == ".json":
        value = json.loads(decoded, object_pairs_hook=_json_object)
        if isinstance(value, dict):
            if set(value) != {"hosts"}:
                raise HostInventoryError("invalid_dataset")
            value = value["hosts"]
        if not isinstance(value, list):
            raise HostInventoryError("invalid_dataset")
        yield from value
    else:
        reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
        headers = next(reader, [])
        if (
            not headers
            or len(set(headers)) != len(headers)
            or not set(headers) <= HOST_FIELDS
            or "address" not in headers
        ):
            raise HostInventoryError("invalid_dataset")
        for values in reader:
            if not values:
                continue
            if len(values) != len(headers):
                raise HostInventoryError("invalid_dataset")
            yield dict(zip(headers, values, strict=True))


def _nmap_rows(raw: bytes) -> Iterator[dict]:
    # Nmap emits a plain <!DOCTYPE nmaprun>. Permit it, reject entities/external declarations.
    if b"\x00" in raw or re.search(rb"<!ENTITY|<!DOCTYPE[^>]*(?:SYSTEM|PUBLIC|\[)", raw, re.I):
        raise HostInventoryError("invalid_dataset")
    tree = ET.fromstring(raw)
    if tree.tag != "nmaprun":
        raise HostInventoryError("invalid_dataset")
    for host in tree.findall("host"):
        status = host.find("status")
        if status is None or status.get("state") != "up":
            continue
        reason = status.get("reason", "")
        responsive = sum(
            1
            for port in host.findall("ports/port")
            if port.find("state") is not None and port.find("state").get("state") in {"open", "closed"}
        )
        # -Pn's user-set status is a target assumption, not proof that every CIDR address exists.
        if reason == "user-set" and not responsive:
            continue
        addresses = [
            a.get("addr", "") for a in host.findall("address") if a.get("addrtype") in {"ipv4", "ipv6"}
        ]
        names = [h.get("name", "") for h in host.findall("hostnames/hostname") if h.get("name")]
        if not addresses:
            if not names:
                raise HostInventoryError("invalid_dataset")
            addresses = [names[0]]
        matches = host.findall("os/osmatch")
        os_name = max(matches, key=lambda m: int(m.get("accuracy", "0"))).get("name", "") if matches else ""
        for address in addresses:
            yield {
                "address": address,
                "hostname": names[0] if names else "",
                "os": os_name,
                "status": "reachable" if responsive or reason not in {"", "user-set"} else "observed",
                "evidence": f"Nmap recorded this host as up (reason: {reason or 'unspecified'}); "
                f"{responsive} open/closed port responses. OS is a scanner estimate when supplied.",
            }


def import_host_file(
    *,
    store: HostInventory,
    workspace: Path,
    file_path: str,
    agent_id: str,
    agent_name: str,
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Validate the entire selected file and commit all rows together, never truncate."""
    cancelled = cancelled or (lambda: False)
    try:
        parts = _parts(file_path)
        if not isinstance(workspace, Path) or not workspace.is_absolute():
            raise HostInventoryError("storage_unavailable")
        source_path = "/workspace/" + "/".join(parts)
        with ExitStack() as stack:
            directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            root_info = os.fstat(directory)
            ancestors = []
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                stack.callback(os.close, child)
                ancestors.append((directory, part, _identity(os.fstat(child))))
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            handle = stack.enter_context(os.fdopen(descriptor, "rb"))
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise HostInventoryError("unsafe_storage")
            if before.st_size > MAX_IMPORT_BYTES:
                raise HostInventoryError("content_limit")
            if cancelled():
                raise HostInventoryError("cancelled")
            raw = handle.read(MAX_IMPORT_BYTES + 1)
            if len(raw) > MAX_IMPORT_BYTES:
                raise HostInventoryError("content_limit")
            digest = hashlib.sha256(raw).hexdigest()

            def verify() -> None:
                if cancelled():
                    raise HostInventoryError("cancelled")
                if (
                    len(raw) != before.st_size
                    or _fingerprint(os.fstat(handle.fileno())) != _fingerprint(before)
                    or _fingerprint(os.stat(parts[-1], dir_fd=directory, follow_symlinks=False))
                    != _fingerprint(before)
                    or _identity(os.stat(workspace, follow_symlinks=False)) != _identity(root_info)
                    or any(
                        _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected
                        for parent, name, expected in ancestors
                    )
                ):
                    raise HostInventoryError("source_changed")

            verify()
            extension = Path(parts[-1]).suffix.lower()

            def rows() -> Iterator[dict]:
                iterator = _nmap_rows(raw) if extension == ".xml" else _normalized_rows(raw, extension)
                for index, row in enumerate(iterator, 1):
                    if not isinstance(row, dict) or not set(row) <= HOST_FIELDS:
                        raise HostInventoryError("invalid_dataset")
                    row = dict(row)
                    original = row.get("evidence", "")
                    if not isinstance(original, str):
                        raise HostInventoryError("invalid_dataset")
                    row["evidence"] = (
                        f"{original}\n" if original else "Host observation imported from structured record.\n"
                    ) + f"Evidence: {source_path}; record {index}; SHA-256 {digest}."
                    if not row.get("source"):
                        row["source"] = source_path
                    yield row

            result = store.import_rows(
                rows(),
                agent_id=agent_id,
                agent_name=agent_name,
                cancelled=cancelled,
                before_commit=verify,
            )
            return {**result, "source": source_path, "sha256": digest}
    except HostInventoryError:
        raise
    except (csv.Error, UnicodeError, ValueError, TypeError, ET.ParseError, RecursionError):
        raise HostInventoryError("invalid_dataset") from None
    except OSError:
        raise HostInventoryError("storage_unavailable") from None
