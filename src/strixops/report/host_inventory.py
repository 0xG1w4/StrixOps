"""Run-owned host observations, independent from the mutable run record.

Writers use a process lock and atomic replacement; readers never create files.
Addresses identify observations, not proven machine identity across networks.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_HOSTS = 50_000
MAX_RELATIONS = 100_000
MAX_SOURCES = 500
HOST_FIELDS = {
    "address",
    "hostname",
    "network_context",
    "subnet",
    "os",
    "role",
    "status",
    "source",
    "evidence",
}
_LIMITS = {
    "address": 253,
    "hostname": 253,
    "network_context": 256,
    "subnet": 128,
    "os": 256,
    "role": 256,
    "status": 20,
    "source": 2048,
    "evidence": 8192,
    "agent_id": 256,
    "agent_name": 256,
}
_STATUSES = {"observed", "reachable"}
_RELATIONS = {"connectivity", "pivot", "trust", "other"}
_FILE = "host_inventory.json"
_LOCK = ".host_inventory.lock"


class HostInventoryError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Host inventory operation failed: {code}")


def text_field(value: Any, field: str, *, required: bool = False) -> str:
    allowed_controls = "\n\r\t" if field == "evidence" else ""
    if (
        not isinstance(value, str)
        or len(value) > _LIMITS[field]
        or any(ord(c) < 32 and c not in allowed_controls for c in value)
    ):
        raise HostInventoryError("invalid_arguments")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise HostInventoryError("invalid_arguments") from None
    value = value.strip()
    if required and not value:
        raise HostInventoryError("invalid_arguments")
    return value


def canonical_address(value: str) -> tuple[str, str]:
    value = text_field(value, "address", required=True)
    try:
        address = ipaddress.ip_address(value)
        if "%" in value:
            raise HostInventoryError("invalid_arguments")
        return str(address), str(address)
    except ValueError:
        pass
    # Never accept CIDRs, URLs, ports, abbreviated numeric IPv4, or wildcard scopes as hosts.
    try:
        hostname = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise HostInventoryError("invalid_arguments") from None
    if (
        len(hostname) > 253
        or not hostname
        or re.fullmatch(r"[0-9.]+", hostname)
        or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in hostname.split("."))
    ):
        raise HostInventoryError("invalid_arguments")
    return hostname, ""


def normalize_host(row: Any) -> dict[str, str]:
    if not isinstance(row, dict) or not set(row) <= HOST_FIELDS or "address" not in row:
        raise HostInventoryError("invalid_arguments")
    result = {field: text_field(row.get(field, ""), field) for field in HOST_FIELDS}
    result["address"], result["ip"] = canonical_address(result["address"])
    if result["hostname"]:
        result["hostname"], ip = canonical_address(result["hostname"])
        if ip:
            raise HostInventoryError("invalid_arguments")
    elif not result["ip"]:
        result["hostname"] = result["address"]
    if result["subnet"]:
        try:
            network = ipaddress.ip_network(result["subnet"], strict=True)
            if result["ip"] and ipaddress.ip_address(result["ip"]) not in network:
                raise ValueError("address outside subnet")
            result["subnet"] = str(network)
        except ValueError:
            raise HostInventoryError("invalid_arguments") from None
    result["status"] = result["status"] or "observed"
    if result["status"] not in _STATUSES or not result["evidence"]:
        raise HostInventoryError("invalid_arguments")
    return result


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _safe_info(directory: int, name: str) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise HostInventoryError("unsafe_storage")
    return info


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _valid_time(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise HostInventoryError("invalid_store")
        result[key] = value
    return result


class HostInventory:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir).absolute()

    def _id(self, kind: str, *values: Any) -> str:
        digest = hashlib.sha256(
            json.dumps([self.run_dir.name, *values], ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        return f"{kind}-{digest}"

    @contextmanager
    def _directory(self):
        try:
            descriptor = os.open(self.run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise HostInventoryError("storage_unavailable") from exc
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def _check_directory(self, directory: int) -> None:
        if _identity(os.stat(self.run_dir, follow_symlinks=False)) != _identity(os.fstat(directory)):
            raise HostInventoryError("storage_unavailable")

    @contextmanager
    def _writer(self, cancelled: Callable[[], bool]):
        with self._directory() as directory:
            _safe_info(directory, _LOCK)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                descriptor = os.open(_LOCK, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            except FileExistsError:
                descriptor = os.open(_LOCK, flags, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise HostInventoryError("unsafe_storage")
                deadline = time.monotonic() + 15
                while True:
                    if cancelled():
                        raise HostInventoryError("cancelled")
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise HostInventoryError("storage_unavailable") from None
                        time.sleep(0.01)
                current = _safe_info(directory, _LOCK)
                if current is None or _identity(current) != _identity(info):
                    raise HostInventoryError("storage_unavailable")
                self._check_directory(directory)
                yield directory
            finally:
                os.close(descriptor)

    def _validate(self, data: Any) -> dict:
        try:
            if (
                not isinstance(data, dict)
                or type(data.get("schema_version")) is not int
                or data.get("schema_version") != 1
                or not isinstance(data.get("hosts"), list)
                or not isinstance(data.get("relations"), list)
                or len(data["hosts"]) > MAX_HOSTS
                or len(data["relations"]) > MAX_RELATIONS
            ):
                raise HostInventoryError("invalid_store")
            ids = set()
            for host in data["hosts"]:
                normalized = normalize_host(
                    {field: host.get(field, "") for field in HOST_FIELDS - {"source", "evidence"}}
                    | {"evidence": "validate"}
                )
                if (
                    host["id"] != self._id("host", normalized["network_context"], normalized["address"])
                    or host["id"] in ids
                    or host["ip"] != normalized["ip"]
                    or any(host[field] != normalized[field] for field in HOST_FIELDS - {"source", "evidence"})
                    or not _valid_time(host["first_seen"])
                    or not _valid_time(host["last_seen"])
                    or not isinstance(host["sources"], list)
                    or not 1 <= len(host["sources"]) <= MAX_SOURCES
                ):
                    raise HostInventoryError("invalid_store")
                ids.add(host["id"])
                for source in host["sources"]:
                    for field in ("source", "evidence", "agent_id", "agent_name"):
                        text_field(source[field], field, required=field == "evidence")
                    if not _valid_time(source["observed_at"]):
                        raise HostInventoryError("invalid_store")
            relation_ids = set()
            for relation in data["relations"]:
                if (
                    relation["source_host_id"] not in ids
                    or relation["target_host_id"] not in ids
                    or relation["source_host_id"] == relation["target_host_id"]
                    or relation["relation_type"] not in _RELATIONS
                    or type(relation["verified"]) is not bool
                    or not _valid_time(relation["observed_at"])
                    or relation["id"] in relation_ids
                ):
                    raise HostInventoryError("invalid_store")
                for field in ("source", "evidence", "agent_id"):
                    text_field(relation[field], field, required=field == "evidence")
                expected = self._id(
                    "relation",
                    *[
                        relation[field]
                        for field in (
                            "source_host_id",
                            "target_host_id",
                            "relation_type",
                            "verified",
                            "evidence",
                            "source",
                            "agent_id",
                        )
                    ],
                )
                if relation["id"] != expected:
                    raise HostInventoryError("invalid_store")
                relation_ids.add(relation["id"])
            return data
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HostInventoryError("invalid_store") from None

    def _read(self, directory: int) -> dict:
        info = _safe_info(directory, _FILE)
        if info is None:
            return {"schema_version": 1, "hosts": [], "relations": [], "source_status": "missing"}
        if info.st_size > MAX_FILE_BYTES:
            raise HostInventoryError("content_limit")
        descriptor = os.open(_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise HostInventoryError("unsafe_storage")
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise HostInventoryError("content_limit")
        try:

            def reject_constant(value: str) -> None:
                raise HostInventoryError("invalid_store")

            data = json.loads(raw, object_pairs_hook=_no_duplicates, parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise HostInventoryError("invalid_store") from None
        return {**self._validate(data), "source_status": "available"}

    def snapshot(self) -> dict:
        try:
            with self._directory() as directory:
                return self._read(directory)
        except OSError as exc:
            raise HostInventoryError("storage_unavailable") from exc

    def _save(self, directory: int, data: dict, cancelled: Callable[[], bool]) -> None:
        content = {key: data[key] for key in ("schema_version", "hosts", "relations")}
        raw = json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        if len(raw) > MAX_FILE_BYTES:
            raise HostInventoryError("content_limit")
        name = f".host-inventory-{uuid.uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if cancelled():
                raise HostInventoryError("cancelled")
            self._check_directory(directory)
            _safe_info(directory, _FILE)
            os.replace(name, _FILE, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory)

    def import_rows(
        self,
        rows: Iterable[dict],
        *,
        agent_id: str,
        agent_name: str,
        cancelled: Callable[[], bool] | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> dict:
        cancelled = cancelled or (lambda: False)
        agent_id, agent_name = text_field(agent_id, "agent_id"), text_field(agent_name, "agent_name")
        try:
            with self._writer(cancelled) as directory:
                data = self._read(directory)
                hosts = {host["id"]: host for host in data["hosts"]}
                count, created = 0, 0
                now = _timestamp()
                for raw in rows:
                    if cancelled():
                        raise HostInventoryError("cancelled")
                    count += 1
                    if count > MAX_HOSTS:
                        raise HostInventoryError("content_limit")
                    row = normalize_host(raw)
                    host_id = self._id("host", row["network_context"], row["address"])
                    observation = {
                        "source": row.pop("source"),
                        "evidence": row.pop("evidence"),
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "observed_at": now,
                    }
                    if host_id not in hosts:
                        if len(hosts) >= MAX_HOSTS:
                            raise HostInventoryError("content_limit")
                        hosts[host_id] = {
                            "id": host_id,
                            **row,
                            "sources": [],
                            "first_seen": now,
                            "last_seen": now,
                        }
                        created += 1
                    host = hosts[host_id]
                    for field in ("hostname", "subnet", "os", "role"):
                        if row[field]:
                            host[field] = row[field]
                    if row["status"] == "reachable":
                        host["status"] = "reachable"
                    host["last_seen"] = now
                    same = next(
                        (
                            item
                            for item in host["sources"]
                            if all(
                                item[field] == observation[field]
                                for field in ("source", "evidence", "agent_id", "agent_name")
                            )
                        ),
                        None,
                    )
                    if same is not None:
                        same["observed_at"] = now
                    else:
                        if len(host["sources"]) >= MAX_SOURCES:
                            raise HostInventoryError("content_limit")
                        host["sources"].append(observation)
                data["hosts"] = list(hosts.values())
                if before_commit is not None:
                    before_commit()
                self._save(directory, data, cancelled)
                return {"success": True, "imported": count, "created": created, "total": len(hosts)}
        except OSError as exc:
            raise HostInventoryError("storage_unavailable") from exc

    def record_host(self, *, agent_id: str = "", agent_name: str = "", **row: Any) -> dict:
        normalized = normalize_host(row)
        result = self.import_rows([row], agent_id=agent_id, agent_name=agent_name)
        return {**result, "host_id": self._id("host", normalized["network_context"], normalized["address"])}

    def record_host_relation(
        self,
        *,
        source_host_id: str,
        target_host_id: str,
        relation_type: str,
        verified: bool,
        evidence: str,
        source: str = "",
        agent_id: str = "",
        agent_name: str = "",
    ) -> dict:
        if (
            not isinstance(source_host_id, str)
            or not isinstance(target_host_id, str)
            or not isinstance(relation_type, str)
            or relation_type not in _RELATIONS
            or type(verified) is not bool
            or source_host_id == target_host_id
        ):
            raise HostInventoryError("invalid_arguments")
        evidence = text_field(evidence, "evidence", required=True)
        source, agent_id = text_field(source, "source"), text_field(agent_id, "agent_id")
        agent_name = text_field(agent_name, "agent_name")
        relation_id = self._id(
            "relation",
            source_host_id,
            target_host_id,
            relation_type,
            verified,
            evidence,
            source,
            agent_id,
        )
        with self._writer(lambda: False) as directory:
            data = self._read(directory)
            ids = {host["id"] for host in data["hosts"]}
            if source_host_id not in ids or target_host_id not in ids:
                raise HostInventoryError("unknown_host")
            relation = {
                "id": relation_id,
                "source_host_id": source_host_id,
                "target_host_id": target_host_id,
                "relation_type": relation_type,
                "verified": verified,
                "evidence": evidence,
                "source": source,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "observed_at": _timestamp(),
            }
            existing = next((r for r in data["relations"] if r["id"] == relation_id), None)
            if existing:
                existing.update(relation)
            else:
                if len(data["relations"]) >= MAX_RELATIONS:
                    raise HostInventoryError("content_limit")
                data["relations"].append(relation)
            self._save(directory, data, lambda: False)
        return {"success": True, "relation_id": relation_id}

    def list_hosts(self, *, query: str = "", limit: int = 50, offset: int = 0) -> dict:
        query = text_field(query, "source").lower()
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise HostInventoryError("invalid_arguments")
        data = self.snapshot()
        hosts = [
            host
            for host in data["hosts"]
            if not query
            or any(
                query in host[field].lower()
                for field in ("address", "hostname", "network_context", "subnet", "os", "role")
            )
        ]
        return {
            "success": True,
            "hosts": copy.deepcopy(hosts[offset : offset + limit]),
            "total": len(hosts),
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < len(hosts),
            "source_status": data["source_status"],
        }
