"""Structured internal host observations shared by agents in one run."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Literal

from agents import RunContextWrapper, function_tool

from strixops.engine.scanconfig import EngineContext
from strixops.report.host_import import import_host_file
from strixops.report.host_inventory import HostInventory, HostInventoryError
from strixops.tools.credentials import _run_in_thread


def _failure(ctx: Any, error: Exception) -> str:
    return json.dumps({"success": False, "error": "invalid_arguments"})


def _store(ctx: RunContextWrapper[EngineContext]) -> HostInventory:
    state = ctx.context.run_state
    spec = getattr(ctx.context.services, "spec", None)
    scan_type = getattr(spec, "scan_type", None) or (
        state.run_record.get("scan_config", {}).get("scan_type") if state is not None else None
    )
    if state is None or scan_type != "internal":
        raise HostInventoryError("internal_scan_required")
    return HostInventory(state.run_dir)


async def _invoke(ctx: RunContextWrapper[EngineContext], operation: str, **kwargs: Any) -> str:
    try:
        store = _store(ctx)
        if operation != "list_hosts":
            kwargs.update(agent_id=ctx.context.agent_id, agent_name=ctx.context.agent_name)
        result = await _run_in_thread(getattr(store, operation), **kwargs)
    except HostInventoryError as exc:
        result = {"success": False, "error": exc.code}
    except Exception:
        result = {"success": False, "error": "storage_unavailable"}
    return json.dumps(result, ensure_ascii=False)


@function_tool(strict_mode=False, failure_error_function=_failure)
async def record_host(
    ctx: RunContextWrapper[EngineContext],
    address: str,
    evidence: str,
    hostname: str = "",
    network_context: str = "",
    subnet: str = "",
    os: str = "",
    role: str = "",
    status: Literal["observed", "reachable"] = "observed",
    source: str = "",
) -> str:
    """Save one discovered internal host immediately, even with no findings or credentials.

    address is one observed IP or DNS hostname, never a URL/CIDR. Preserve the
    evidence and source. reachable requires an observed successful response;
    an address mentioned in a config is only observed. Record newly discovered
    hosts across all subnets without expanding testing authorization. Do not
    infer subnet masks, OS, roles, hostname/IP equivalence or access. Supply
    network_context only when explicitly known; empty stays scoped to this run.
    Return host_id is used for relations. Use import_hosts for complete datasets.
    This records data; it never scans or generates/updates a topology snapshot.
    """
    return await _invoke(
        ctx,
        "record_host",
        address=address,
        evidence=evidence,
        hostname=hostname,
        network_context=network_context,
        subnet=subnet,
        os=os,
        role=role,
        status=status,
        source=source,
    )


@function_tool(strict_mode=False, failure_error_function=_failure)
async def import_hosts(ctx: RunContextWrapper[EngineContext], file_path: str) -> str:
    """Import every host from a saved output JSON/CSV or Nmap XML, atomically.

    file_path must be /workspace/output/... or output/... in this run. Keep the
    original scanner output as evidence. Normalized JSON is a list of host
    objects or {"hosts": [...]}; CSV requires address. Optional fields: hostname,
    network_context, subnet, os, role, status (observed/reachable), source, evidence.
    Nmap imports observed up hosts and skips -Pn user-set targets with no actual
    open/closed response. No subnet is inferred. Metadata retains file/hash/row
    provenance. Symlinks/hard links, changed files, invalid rows, >32 MiB files,
    or >50,000 records fail the entire import. Split oversized files explicitly
    and account for all parts; the registry holds at most 50,000 distinct hosts.
    Report capacity failures instead of sampling or claiming completeness. Inspect returned
    counts and list_hosts. No scans run and no topology snapshot is generated.
    """
    try:
        store = _store(ctx)
        workspace = ctx.context.run_state.run_record.get("workspace", {})
        path = workspace.get("path") if isinstance(workspace, dict) else None
        if not isinstance(path, str) or not path or "\x00" in path:
            raise HostInventoryError("storage_unavailable")
        cancellation = threading.Event()
        result = await _run_in_thread(
            import_host_file,
            cancel_event=cancellation,
            store=store,
            workspace=Path(path),
            file_path=file_path,
            agent_id=ctx.context.agent_id,
            agent_name=ctx.context.agent_name,
            cancelled=cancellation.is_set,
        )
    except HostInventoryError as exc:
        result = {"success": False, "error": exc.code}
    except Exception:
        result = {"success": False, "error": "storage_unavailable"}
    return json.dumps(result, ensure_ascii=False)


@function_tool(strict_mode=False, failure_error_function=_failure)
async def record_host_relation(
    ctx: RunContextWrapper[EngineContext],
    source_host_id: str,
    target_host_id: str,
    relation_type: Literal["connectivity", "pivot", "trust", "other"],
    verified: bool,
    evidence: str,
    source: str = "",
) -> str:
    """Record an evidenced relationship between two saved host IDs in this run.

    Direction is source_host_id to target_host_id. verified=true requires an
    actual supporting check. Same subnet, a found credential or a tunnel name
    never proves a connection, trust, login or compromise. Label inferred or
    untested observations verified=false and explain their limits in evidence.
    Author/time are assigned from trusted context; no network action is executed.
    """
    return await _invoke(
        ctx,
        "record_host_relation",
        source_host_id=source_host_id,
        target_host_id=target_host_id,
        relation_type=relation_type,
        verified=verified,
        evidence=evidence,
        source=source,
    )


@function_tool(strict_mode=False, failure_error_function=_failure)
async def list_hosts(
    ctx: RunContextWrapper[EngineContext],
    query: str = "",
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Read this run's saved hosts and IDs with bounded pagination (limit 1–100).

    Query matches address, hostname, context, subnet, OS or role. Verify imports
    through counts and targeted queries; avoid loading the entire inventory into
    the model context. Presence alone does not prove reachability or access.
    """
    return await _invoke(ctx, "list_hosts", query=query, limit=limit, offset=offset)
