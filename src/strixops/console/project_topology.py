"""Manually published project topology snapshots over recorded internal observations.

Reading checks source revisions but never builds a graph. Generation is deterministic
apart from version/time; no scanner, DNS resolver or model is invoked. Secrets stay
in the original credential inventory and are resolved only on an explicit reveal.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from strixops.console import credentials, json_store
from strixops.engine.targets import MAX_TARGETS
from strixops.report.host_inventory import HostInventory, HostInventoryError, canonical_address

OpenFile = Callable[[Path, str], int]
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_NODES = 100_000
MAX_RECORDS = 200_000
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SEVERITY = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1, "": 0}
_TEXT_FIELDS = {"title", "description", "content", "evidence", "source", "validation_evidence", "os", "role"}
_CIDR = re.compile(r"[0-9a-fA-F:.]+/[0-9]{1,3}\Z")
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _open_file(run_dir: Path, relative: str) -> int:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts or "\x00" in relative:
        raise ValueError("invalid source path")
    directory = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(descriptor)
            raise ValueError("unsafe source file")
        return descriptor
    finally:
        os.close(directory)


def _text(run: Path, relative: str, opener: OpenFile) -> str:
    with os.fdopen(opener(run, relative), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_INPUT_BYTES:
            raise ValueError("invalid source file")
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("source exceeds limit")
    return raw.decode("utf-8")


def _json(run: Path, relative: str, opener: OpenFile) -> Any:
    def reject(_value: str) -> None:
        raise ValueError("invalid JSON constant")

    return json.loads(_text(run, relative, opener), parse_constant=reject)


def _warning(warnings: list[dict], code: str, run: str = "") -> None:
    row = {"code": code, **({"run": run} if run else {})}
    if row not in warnings:
        warnings.append(row)


def _id(*values: Any) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _path(project_id: str, storage_root: Path | None) -> Path:
    if not _PROJECT_ID.fullmatch(project_id) or project_id in {".", ".."}:
        raise ValueError("invalid project id")
    root = storage_root or Path(
        os.environ.get("STRIXOPS_PROJECT_TOPOLOGIES_DIR")
        or (Path.home() / ".strixops" / "project_topologies")
    )
    return Path(root).expanduser() / f"{project_id}.json"


def _source_state(run_dirs: Iterable[Path], opener: OpenFile) -> tuple[list[Path], str, list[dict]]:
    """Use bounded file metadata for cheap stale checks, not graph reconstruction."""
    runs, warnings, stamps = [], [], []
    for run in sorted({Path(path).absolute() for path in run_dirs}, key=lambda path: path.name):
        try:
            record = _json(run, "run.json", opener)
            config = record.get("scan_config", {})
            if not isinstance(config, dict):
                raise ValueError("invalid scan configuration")
            if config.get("scan_type") != "internal":
                continue
            runs.append(run)
            files = []

            def traversal_error(error: OSError, name: str = run.name, entries: list = files) -> None:
                _warning(warnings, "source_unreadable", name)
                entries.append(["unreadable_directory", str(error.filename)])

            base = os.open(run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for relative, directories, names, descriptor in os.fwalk(
                    ".",
                    dir_fd=base,
                    follow_symlinks=False,
                    onerror=traversal_error,
                ):
                    prefix = Path(relative)
                    if prefix == Path("."):
                        directories[:] = sorted(
                            set(directories) & {".state", "internal_findings", "vulnerabilities", "evidence"}
                        )
                        names = [
                            name
                            for name in names
                            if name
                            in {
                                "run.json",
                                "host_inventory.json",
                                "vulnerabilities.json",
                                "assessment.json",
                                "credentials.json",
                                "credentials.csv",
                            }
                        ]
                    elif prefix == Path(".state"):
                        directories[:] = []
                        names = [
                            name for name in names if name.startswith("credentials.") or name == "notes.json"
                        ]
                    elif prefix.parts[0] in {"internal_findings", "vulnerabilities"}:
                        directories[:] = []
                        names = [name for name in names if name.endswith(".md")]
                    for name in sorted(directories):
                        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        files.append(["directory", (prefix / name).as_posix(), info.st_mode, info.st_ino])
                        if not stat.S_ISDIR(info.st_mode):
                            _warning(warnings, "source_unreadable", run.name)
                    for name in sorted(names):
                        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        files.append(
                            [
                                (prefix / name).as_posix(),
                                info.st_size,
                                info.st_mtime_ns,
                                info.st_ctime_ns,
                                info.st_mode,
                                info.st_ino,
                            ]
                        )
                        if len(files) > MAX_RECORDS:
                            raise ValueError("too many source files")
            finally:
                os.close(base)
            stamps.append([run.name, files])
        except (OSError, ValueError, AttributeError, TypeError, RecursionError):
            _warning(warnings, "source_unreadable", run.name)
            stamps.append([run.name, "unreadable"])
    return runs, _id(stamps), warnings


def _stored(path: Path) -> dict | None:
    data = json_store.read(path)
    if not data:
        return None
    if (
        data.get("schema_version") != 1
        or not isinstance(data.get("snapshot"), dict)
        or not isinstance(data["snapshot"].get("nodes"), list)
        or not isinstance(data["snapshot"].get("edges"), list)
        or type(data["snapshot"].get("version")) is not int
        or data["snapshot"]["version"] < 1
    ):
        raise ValueError("invalid topology snapshot")
    return data["snapshot"]


def read_topology(
    project_id: str,
    run_dirs: Iterable[Path],
    *,
    storage_root: Path | None = None,
    open_file: OpenFile | None = None,
) -> dict:
    path = _path(project_id, storage_root)
    runs, fingerprint, warnings = _source_state(run_dirs, open_file or _open_file)
    snapshot = _stored(path)
    for row in snapshot.get("warnings", []) if snapshot else []:
        if row not in warnings:
            warnings.append(row)
    return {
        "snapshot": snapshot,
        "eligible_runs": len(runs),
        "stale": bool(snapshot and snapshot.get("source_fingerprint") != fingerprint),
        "warnings": warnings,
        "source_status": "partial" if warnings else "available" if runs else "missing",
    }


def topology_status(
    run_dirs: Iterable[Path],
    source_fingerprint: str = "",
    *,
    open_file: OpenFile | None = None,
) -> dict:
    """Lightweight polling: no snapshot read, graph construction or secret loading."""
    runs, current, warnings = _source_state(run_dirs, open_file or _open_file)
    return {
        "eligible_runs": len(runs),
        "stale": bool(source_fingerprint and current != source_fingerprint),
        "warnings": warnings,
        "source_status": "partial" if warnings else "available" if runs else "missing",
    }


def _address(raw: Any) -> tuple[str, str] | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    try:
        # URL/host:port normalization is syntactic only: never resolve DNS.
        if "://" in value:
            value = urlsplit(value).hostname or ""
        elif value.startswith("[") or (value.count(":") == 1 and value.rsplit(":", 1)[1].isdigit()):
            value = urlsplit("//" + value).hostname or ""
        return canonical_address(value)
    except (ValueError, UnicodeError):
        return None


def _redact(value: Any, secrets: list[str], key: str = "") -> Any:
    if isinstance(value, dict):
        return {name: _redact(item, secrets, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets, key) for item in value]
    if isinstance(value, str) and key in _TEXT_FIELDS:
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
    return value


def _findings(run: Path, opener: OpenFile, warnings: list[dict]) -> tuple[list[dict], list[dict]]:
    vulnerabilities, findings = [], []
    try:
        data = _json(run, "vulnerabilities.json", opener)
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise ValueError("invalid vulnerabilities")
        vulnerabilities = data
    except FileNotFoundError:
        pass
    except (OSError, ValueError, RecursionError):
        _warning(warnings, "findings_unreadable", run.name)
    directory = None
    try:
        base = os.open(run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.open(
                "internal_findings", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base
            )
        finally:
            os.close(base)
        names = sorted(name for name in os.listdir(directory) if name.endswith(".md"))
        if len(names) > MAX_RECORDS:
            raise ValueError("too many findings")
        for name in names:
            try:
                content = _text(run, f"internal_findings/{name}", opener)
                row = {"id": Path(name).stem, "source_file": name}
                for line in content.split("\n## ", 1)[0].splitlines():
                    if line.startswith("# "):
                        row.setdefault("title", line[2:].strip())
                    match = re.match(r"\*\*(Host|Severity|Type|Source):\*\*\s*(.*)", line)
                    if match:
                        field = {"Type": "finding_type"}.get(match[1], match[1].lower())
                        row[field] = match[2].strip()
                findings.append(row)
            except (OSError, ValueError):
                _warning(warnings, "findings_unreadable", run.name)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        _warning(warnings, "findings_unreadable", run.name)
    finally:
        if directory is not None:
            os.close(directory)
    if len(vulnerabilities) + len(findings) > MAX_RECORDS:
        raise ValueError("topology source record limit exceeded")
    return vulnerabilities, findings


def _record_view(row: dict, run: str) -> dict:
    fields = ("id", "title", "severity", "description", "host", "target", "finding_type", "source_file")
    result = {
        **{key: row[key] for key in fields if isinstance(row.get(key), str)},
        "severity": str(row.get("severity") or "info").lower(),
        "source_run": run,
    }
    # Targets may be URLs containing authentication material or tokens. Keep
    # only the parsed host here; the original finding stays in its source task.
    for field in ("target", "host"):
        if field in result:
            normalized = _address(result[field])
            result[field] = normalized[0] if normalized else ""
    return result


def _cidr(value: Any, *, strict: bool) -> Network | None:
    """Only complete address/prefix tokens qualify; never extract from prose or URLs."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    value = value.strip()
    if not _CIDR.fullmatch(value):
        return None
    try:
        return ipaddress.ip_network(value, strict=strict)
    except ValueError:
        return None


def _scan_ranges(config: Any) -> list[Network]:
    if not isinstance(config, dict):
        raise ValueError("invalid scan configuration")
    primary, targets = config.get("target"), config.get("targets", [])
    if targets is None:
        targets = []
    if not isinstance(targets, list) or len(targets) > MAX_TARGETS:
        raise ValueError("invalid recorded target list")
    if primary is not None and not isinstance(primary, str):
        raise ValueError("invalid recorded target")
    if not all(isinstance(target, str) for target in targets):
        raise ValueError("invalid recorded target")
    networks = {}
    for target in [primary, *targets]:
        # Host bits are accepted for a *scan range*, not promoted to a confirmed subnet.
        network = _cidr(target, strict=False)
        if network is not None:
            networks[str(network)] = network
    return sorted(networks.values(), key=lambda network: (-network.prefixlen, str(network)))


def _group_hosts(
    nodes: dict[str, dict],
    node_runs: dict[str, set[str]],
    recorded_subnets: dict[str, set[str]],
    scan_ranges: dict[str, list[Network]],
    warnings: list[dict],
) -> None:
    """Compute display groups without changing recorded host identity or network evidence."""
    for node_id, node in nodes.items():
        node.update(group_cidr="", group_basis="unassigned")
        if not node["ip"]:
            continue
        address = ipaddress.ip_address(node["ip"])
        recorded = {
            str(network): network
            for value in recorded_subnets[node_id]
            if (network := _cidr(value, strict=True)) is not None
            and network.version == address.version
            and address in network
        }
        if len(recorded) == 1:
            node.update(group_cidr=next(iter(recorded)), group_basis="recorded_subnet")
            continue
        if len(recorded) > 1:
            # Even nested CIDRs disagree about a host's actual mask. Keep the evidence,
            # but use a labelled display fallback instead of claiming either is confirmed.
            node["group_conflict"] = True
            _warning(warnings, "recorded_subnet_conflict")
        matches = (
            network
            for run in node_runs[node_id]
            for network in scan_ranges.get(run, [])
            if network.version == address.version and address in network
        )
        network = min(matches, key=lambda value: (-value.prefixlen, str(value)), default=None)
        if network is not None:
            node.update(group_cidr=str(network), group_basis="scan_range")
        else:
            prefix = 24 if address.version == 4 else 64
            node.update(
                group_cidr=str(ipaddress.ip_network(f"{address}/{prefix}", strict=False)),
                group_basis="address_group",
            )


def _build(runs: list[Path], opener: OpenFile, warnings: list[dict]) -> tuple[list[dict], list[dict]]:
    documents: dict[str, dict] = {}
    known_names: dict[tuple[str, str], set[str]] = defaultdict(set)
    project_secrets: set[str] = set()
    scan_ranges: dict[str, list[Network]] = {}
    range_count = 0
    for run in runs:
        try:
            config = _json(run, "run.json", opener).get("scan_config", {})
            scan_ranges[run.name] = _scan_ranges(config)
        except (OSError, ValueError, AttributeError, TypeError, RecursionError):
            scan_ranges[run.name] = []
            _warning(warnings, "scan_ranges_unreadable", run.name)
        range_count += len(scan_ranges[run.name])
        if range_count > MAX_RECORDS:
            raise ValueError("topology scan range limit exceeded")
        try:
            inventory = HostInventory(run).snapshot()
        except (HostInventoryError, OSError, ValueError):
            inventory = {"hosts": [], "relations": [], "source_status": "unreadable"}
            _warning(warnings, "host_inventory_unreadable", run.name)
        if inventory.get("source_status") == "missing":
            _warning(warnings, "legacy_inventory", run.name)
        vulns, findings = _findings(run, opener, warnings)
        credential_rows = []
        try:
            credential_inventory, reader = credentials.load_inventory(run, opener)
            for row in credential_inventory.iter_credentials():
                credential_rows.append(row)
                if len(credential_rows) > MAX_RECORDS:
                    raise ValueError("too many credentials")
            if credentials.source_state(credential_inventory, reader)["warnings"]:
                _warning(warnings, "credentials_partial", run.name)
        except (OSError, ValueError, TypeError):
            _warning(warnings, "credentials_unreadable", run.name)
        secrets = sorted(
            {
                row[field]
                for row in credential_rows
                for field in ("password", "hash")
                if isinstance(row.get(field), str) and row[field]
            },
            key=len,
            reverse=True,
        )
        project_secrets.update(secrets)
        observations = []
        try:
            campaign = _json(run, "run.json", opener).get("internal_campaign") or {}
            raw_observations = campaign.get("observations", {})
            if not isinstance(raw_observations, dict):
                raise ValueError("invalid campaign observations")
            for row in raw_observations.values():
                if not isinstance(row, dict) or not isinstance(row.get("details"), dict):
                    raise ValueError("invalid campaign observation")
                if row.get("event_type") in {
                    "host_capability",
                    "egress_observed",
                    "defense_observed",
                    "pivot_verified",
                }:
                    observations.append(
                        {
                            "host": row.get("host"),
                            "source": str(row.get("event_type") or ""),
                            "evidence": str(row["details"].get("evidence") or ""),
                            "observed_at": str(row.get("observed_at") or ""),
                            "agent_id": str(row.get("agent_id") or ""),
                        }
                    )
        except (OSError, ValueError, AttributeError, TypeError, RecursionError):
            _warning(warnings, "observations_unreadable", run.name)
        documents[run.name] = {
            "inventory": _redact(inventory, secrets),
            "vulns": _redact(vulns, secrets),
            "findings": _redact(findings, secrets),
            "credentials": _redact(credential_rows, secrets),
            "observations": observations,
        }
        for host in inventory["hosts"]:
            context = "context:" + host["network_context"] if host["network_context"] else "run:" + run.name
            if host.get("ip") and host.get("hostname"):
                known_names[(context, host["hostname"])].add(host["ip"])

    # A credential discovered by one task may also appear in another task's evidence.
    documents = _redact(documents, sorted(project_secrets, key=len, reverse=True))
    nodes: dict[str, dict] = {}
    aliases: dict[tuple[str, str], set[str]] = defaultdict(set)
    host_ids: dict[tuple[str, str], str] = {}
    node_runs: dict[str, set[str]] = defaultdict(set)
    recorded_subnets: dict[str, set[str]] = defaultdict(set)

    def add_host(host: dict, run: str, *, backfilled: bool = False) -> str:
        context = "context:" + host["network_context"] if host.get("network_context") else "run:" + run
        address = host["address"]
        candidates = known_names.get((context, address), set())
        if not host.get("ip") and len(candidates) == 1:
            address = next(iter(candidates))
        node_id = "node-" + _id(context, address)
        if node_id not in nodes:
            if len(nodes) >= MAX_NODES:
                raise ValueError("topology host limit exceeded")
            normalized = _address(address)
            nodes[node_id] = {
                "id": node_id,
                "address": address,
                "ip": normalized[1] if normalized else "",
                "hostname": host.get("hostname", ""),
                "network_context": host.get("network_context") or run,
                "group_context": host.get("network_context") or "",
                "subnet": host.get("subnet", ""),
                "os": host.get("os", ""),
                "role": host.get("role", ""),
                "status": host.get("status", "observed"),
                "sources": [],
                "first_seen": host.get("first_seen", ""),
                "last_seen": host.get("last_seen", ""),
                "severity": "info",
                "backfilled": backfilled,
                "vulnerabilities": [],
                "findings": [],
                "credentials": [],
            }
        node = nodes[node_id]
        node_runs[node_id].add(run)
        if host.get("subnet"):
            recorded_subnets[node_id].add(host["subnet"])
        for field in ("hostname", "subnet", "os", "role"):
            if not node[field] and host.get(field):
                node[field] = host[field]
        if host.get("status") == "reachable":
            node["status"] = "reachable"
        for field, operation in (("first_seen", min), ("last_seen", max)):
            times = [value for value in (node[field], host.get(field)) if value]
            node[field] = operation(times) if times else ""
        node["backfilled"] = node["backfilled"] and backfilled
        for source in host.get("sources", []):
            value = {
                **{
                    field: source[field]
                    for field in (
                        "source",
                        "evidence",
                        "agent_id",
                        "agent_name",
                        "observed_at",
                    )
                    if isinstance(source.get(field), str)
                },
                "run": run,
            }
            if value not in node["sources"]:
                node["sources"].append(value)
        for value in (address, host.get("address"), host.get("hostname")):
            if value:
                aliases[(run, value)].add(node_id)
        return node_id

    for run, document in documents.items():
        for host in document["inventory"]["hosts"]:
            host_ids[(run, host["id"])] = add_host(host, run)

    def related(raw_address: Any, run: str, source: str) -> dict | None:
        normalized = _address(raw_address)
        if not normalized:
            _warning(warnings, "unmatched_records", run)
            return None
        address, ip = normalized
        matching = aliases.get((run, address), set())
        if len(matching) > 1:
            _warning(warnings, "ambiguous_host", run)
            return None
        if matching:
            node_id = next(iter(matching))
            node_runs[node_id].add(run)
            return nodes[node_id]
        node_id = add_host(
            {
                "address": address,
                "ip": ip,
                "hostname": "" if ip else address,
                "network_context": "",
                "sources": [{"source": source, "evidence": "", "observed_at": ""}],
            },
            run,
            backfilled=True,
        )
        return nodes[node_id]

    for run, document in documents.items():
        for observation in document["observations"]:
            node = related(observation["host"], run, observation["source"])
            if node is not None:
                source = {key: value for key, value in observation.items() if key != "host"}
                source["run"] = run
                if source not in node["sources"]:
                    node["sources"].append(source)
        for field, key in (("vulns", "vulnerabilities"), ("findings", "findings")):
            for row in document[field]:
                node = related(row.get("host") or row.get("target"), run, f"{key}:{row.get('id', '')}")
                if node is None:
                    continue
                view = _record_view(row, run)
                if view not in node[key]:
                    node[key].append(view)
                severity = view["severity"]
                if _SEVERITY.get(severity, 0) > _SEVERITY.get(node["severity"], 0):
                    node["severity"] = severity
        for row in document["credentials"]:
            node = related(row.get("host"), run, f"credential:{row.get('id', '')}")
            if node is None:
                continue
            node["credentials"].append(
                {
                    **{
                        field: row.get(field, "")
                        for field in (
                            "id",
                            "username",
                            "secret_type",
                            "validation_status",
                            "validation_evidence",
                        )
                    },
                    "host": node["address"],
                    "source_run": run,
                    "has_password": bool(row.get("password")) or row.get("secret_type") == "password",
                    "has_hash": bool(row.get("hash")),
                }
            )

    edges = []
    for run, document in documents.items():
        for relation in document["inventory"]["relations"]:
            source = host_ids.get((run, relation["source_host_id"]))
            target = host_ids.get((run, relation["target_host_id"]))
            if not source or not target:
                _warning(warnings, "relation_unmatched", run)
                continue
            edges.append(
                {
                    "id": "edge-" + _id(run, relation["id"]),
                    "source": source,
                    "target": target,
                    "relation_type": relation["relation_type"],
                    "verified": relation["verified"],
                    "evidence": relation["evidence"],
                    "source_run": run,
                }
            )
    for node in nodes.values():
        node["vulnerabilities"].sort(key=lambda row: -_SEVERITY.get(row["severity"], 0))
    _group_hosts(nodes, node_runs, recorded_subnets, scan_ranges, warnings)
    return sorted(
        nodes.values(),
        key=lambda node: (
            node["network_context"],
            node["subnet"],
            _address_sort(node["address"]),
        ),
    ), sorted(edges, key=lambda edge: edge["id"])


def _address_sort(value: str) -> tuple[int, Any]:
    try:
        address = ipaddress.ip_address(value)
        return address.version, int(address)
    except ValueError:
        return 9, value


def generate_topology(
    project_id: str,
    run_dirs: Iterable[Path],
    *,
    storage_root: Path | None = None,
    open_file: OpenFile | None = None,
) -> dict:
    path, opener, run_dirs = _path(project_id, storage_root), open_file or _open_file, list(run_dirs)
    runs, fingerprint, warnings = _source_state(run_dirs, opener)
    if not runs:
        raise ValueError("no internal tasks available")
    # Publication occurs only after all processing succeeds; any failure preserves the old graph.
    nodes, edges = _build(runs, opener, warnings)
    _, after, after_warnings = _source_state(run_dirs, opener)
    if after != fingerprint:
        _warning(warnings, "sources_changed")
    for warning in after_warnings:
        if warning not in warnings:
            warnings.append(warning)
    with json_store.transaction(path) as stored:
        previous = stored.get("snapshot", {})
        if stored and (
            stored.get("schema_version") != 1
            or not isinstance(previous, dict)
            or type(previous.get("version")) is not int
        ):
            raise ValueError("invalid topology snapshot")
        snapshot = {
            "version": previous.get("version", 0) + 1,
            "grouping_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "source_fingerprint": fingerprint,
            "run_names": [run.name for run in runs],
            "nodes": nodes,
            "edges": edges,
            "warnings": warnings,
            "partial": bool(warnings),
        }
        if len(json.dumps(snapshot, ensure_ascii=False).encode()) > MAX_SNAPSHOT_BYTES:
            raise ValueError("topology snapshot limit exceeded")
        stored.clear()
        stored.update(schema_version=1, snapshot=snapshot)
    return read_topology(project_id, run_dirs, storage_root=storage_root, open_file=opener)


def reveal_credential(
    project_id: str,
    run_dirs: Iterable[Path],
    *,
    node_id: str,
    credential_id: str,
    source_run: str,
    version: int,
    storage_root: Path | None = None,
    open_file: OpenFile | None = None,
) -> dict[str, str]:
    snapshot = _stored(_path(project_id, storage_root))
    if snapshot is None or type(version) is not int or version != snapshot["version"]:
        raise ValueError("topology version changed")
    node = next((node for node in snapshot["nodes"] if node["id"] == node_id), None)
    if node is None or not any(
        row["id"] == credential_id and row["source_run"] == source_run for row in node["credentials"]
    ):
        raise ValueError("credential is not associated with this host")
    runs, _, _ = _source_state(run_dirs, open_file or _open_file)
    run = next((run for run in runs if run.name == source_run), None)
    if run is None:
        raise ValueError("credential source is no longer in this project")
    inventory, _ = credentials.load_inventory(run, open_file or _open_file)
    for row in inventory.iter_credentials():
        if row["id"] == credential_id:
            return {"password": str(row.get("password") or ""), "hash": str(row.get("hash") or "")}
    raise ValueError("credential source is no longer available")
