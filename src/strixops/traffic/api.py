"""Additive MCP-task Console API with a local/authenticated access boundary."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import re
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from strixops.traffic.access_credentials import AccessCredentialsError, ensure_token, read_token
from strixops.traffic.security import public_flow
from strixops.traffic.service import get_service
from strixops.traffic.store import storage_root


def _trusted_origins() -> set[str]:
    return {
        item.strip().rstrip("/")
        for item in os.environ.get("STRIXOPS_MCP_TRUSTED_ORIGINS", "").split(",")
        if item.strip()
    }


def bootstrap_host_allowed(scope: dict) -> bool:
    """Only literal Console addresses or configured domains may issue browser credentials."""
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    host = headers.get("host", "")
    scheme = scope.get("scheme", "http")
    if scheme not in {"http", "https"} or not re.fullmatch(
        r"(?:\[[A-Fa-f0-9:.]+\]|[A-Za-z0-9._-]+)(?::[0-9]{1,5})?", host
    ):
        return False
    try:
        parsed = urlsplit(f"{scheme}://{host}")
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
        ):
            return False
        # Reading port also rejects malformed/out-of-range ports and unbracketed IPv6.
        if parsed.hostname == "localhost" or f"{scheme}://{host}" in _trusted_origins():
            return True
        address = ipaddress.ip_address(parsed.hostname)
        return "%" not in str(address)
    except ValueError:
        return False


def origin_error(scope: dict) -> str:
    """Keep browser-origin checks independent from local or token authentication."""
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    host = headers.get("host", "")
    scheme = scope.get("scheme", "http")
    origin = headers.get("origin", "")
    peer = (scope.get("client") or ("", 0))[0]
    local_peer = peer in {"127.0.0.1", "::1", "localhost"}
    expected = f"{scheme}://{host}"
    # Explicitly allow only same-origin browser requests, including behind a trusted access layer.
    trusted_local_origin = local_peer and origin.rstrip("/") in _trusted_origins()
    if origin and origin.rstrip("/") != expected and not trusted_local_origin:
        return "Cross-origin access to MCP tasks is not permitted"
    if headers.get("sec-fetch-site") == "cross-site":
        return "Cross-site access to MCP tasks is not permitted"
    return ""


def access_error(scope: dict) -> str:
    if error := origin_error(scope):
        return error
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    host = headers.get("host", "")
    peer = (scope.get("client") or ("", 0))[0]
    local_peer = peer in {"127.0.0.1", "::1", "localhost"}
    expected = f"{scope.get('scheme', 'http')}://{host}"
    configured = os.environ.get("STRIXOPS_MCP_TOKEN", "")
    supplied = headers.get("x-mcp-token", "")
    if headers.get("authorization", "").startswith("Bearer "):
        supplied = headers["authorization"][7:]
    if configured and hmac.compare_digest(supplied.encode(), configured.encode()):
        return ""
    try:
        hostname = urlsplit("//" + host).hostname
    except ValueError:
        hostname = None
    local_host = hostname in {"localhost", "127.0.0.1", "::1"}
    if peer == "testclient" and hostname == "testserver":
        return ""
    if local_host and local_peer:
        return ""
    if local_peer and expected in _trusted_origins():
        return ""
    if not configured and supplied:
        try:
            automatic = read_token(storage_root())
        except AccessCredentialsError as exc:
            return str(exc)
        if automatic and hmac.compare_digest(supplied.encode(), automatic.encode()):
            return ""
    return "Open the MCP page to initialize browser access, or supply the configured MCP access token"


def ensure_access(request: Request):
    if error := access_error(request.scope):
        raise HTTPException(status_code=403, detail=error)


router = APIRouter(prefix="/api/mcp", tags=["mcp-tasks"], dependencies=[Depends(ensure_access)])
access_router = APIRouter(prefix="/api/mcp", tags=["mcp-access"])


@access_router.get("/access")
def access_status(request: Request):
    """Describe how this browser can authenticate without loading task data."""
    origin_problem = origin_error(request.scope)
    headers = {"Cache-Control": "no-store", "Vary": "Origin, Authorization, X-MCP-Token"}
    if origin_problem:
        return JSONResponse(
            {
                "allowed": False,
                "token_configured": False,
                "reason": "origin_rejected",
                "message": origin_problem,
                "mode": "manual" if os.environ.get("STRIXOPS_MCP_TOKEN", "") else "automatic",
                "bootstrap_available": False,
            },
            status_code=403,
            headers=headers,
        )
    allowed = not access_error(request.scope)
    configured = bool(os.environ.get("STRIXOPS_MCP_TOKEN", ""))
    available = not configured and bootstrap_host_allowed(request.scope)
    reason = (
        ""
        if allowed
        else "token_required"
        if configured
        else "bootstrap_required"
        if available
        else "bootstrap_unavailable"
    )
    message = ""
    if not allowed:
        message = (
            "Enter the MCP access token configured on this server"
            if configured
            else "Initialize MCP access for this browser"
            if available
            else "Open Console using its IP address or configure this Console origin as trusted"
        )
    return JSONResponse(
        {
            "allowed": allowed,
            "token_configured": configured,
            "reason": reason,
            "message": message,
            "mode": "manual" if configured else "automatic",
            "bootstrap_available": available,
        },
        headers=headers,
    )


@access_router.post("/access/bootstrap")
def bootstrap_access(request: Request):
    """Initialize same-origin browser access under the existing Console deployment boundary."""
    headers = {"Cache-Control": "no-store", "Vary": "Origin, X-StrixOps-MCP-Bootstrap"}

    def reject(reason: str, message: str, status: int = 403):
        return JSONResponse(
            {"allowed": False, "reason": reason, "message": message}, status_code=status, headers=headers
        )

    if problem := origin_error(request.scope):
        return reject("origin_rejected", problem)
    expected = f"{request.scope.get('scheme', 'http')}://{request.headers.get('host', '')}"
    origin = request.headers.get("origin", "")
    peer = (request.scope.get("client") or ("", 0))[0]
    # A configured development/access proxy may rewrite Host while preserving
    # the browser Origin. Retain that explicit trust only for a local upstream.
    trusted_local_origin = peer in {"127.0.0.1", "::1", "localhost"} and origin.rstrip(
        "/"
    ) in _trusted_origins()
    if (origin != expected and not trusted_local_origin) or request.headers.get(
        "x-strixops-mcp-bootstrap"
    ) != "1":
        return reject("origin_rejected", "MCP initialization requires a same-origin browser request")
    if os.environ.get("STRIXOPS_MCP_TOKEN", ""):
        return reject(
            "manual_configuration", "This server requires its explicitly configured MCP access token"
        )
    if not bootstrap_host_allowed(request.scope):
        return reject(
            "bootstrap_unavailable", "Open Console using its IP address or configure its trusted origin"
        )
    try:
        token = ensure_token(storage_root())
    except AccessCredentialsError as exc:
        return reject("initialization_failed", str(exc), status=503)
    return JSONResponse({"allowed": True, "token": token, "mode": "automatic"}, headers=headers)


def invoke(function: Callable, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTask(StrictBody):
    name: str = Field(min_length=1, max_length=160)
    allow_hosts: list[str] = Field(default_factory=list, max_length=100)
    exclude_hosts: list[str] = Field(default_factory=list, max_length=100)
    agent_config: dict = Field(default_factory=dict)


class UpdateTask(StrictBody):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    allow_hosts: list[str] | None = Field(default=None, max_length=100)
    exclude_hosts: list[str] | None = Field(default=None, max_length=100)
    agent_config: dict | None = None


class ReplayBody(StrictBody):
    modifications: dict = Field(default_factory=dict)


class TestBody(StrictBody):
    flow_ids: list[str] = Field(min_length=1, max_length=50)
    skills: list[str] | None = None
    instruction: str | None = Field(default=None, max_length=8000)
    profile_id: str | None = None
    max_requests: int | None = Field(default=None, ge=1, le=100)
    max_seconds: int | None = Field(default=None, ge=5, le=900)


@router.get("/catalog")
def catalog():
    from strixops.console import settings_store
    from strixops.traffic.prompts import (
        BASE_SKILLS,
        CAPABILITIES,
        LIMITATIONS,
        REQUEST_CONTRACT,
        compatible_skills,
        normalized_config,
    )

    data = settings_store.load_settings()
    return {
        "skills": compatible_skills(),
        "profiles": [{"id": p["id"], "name": p.get("name", p["id"])} for p in data["profiles"]],
        "active_profile_id": data.get("active_profile_id", ""),
        "default_skills": list(BASE_SKILLS),
        "capabilities": CAPABILITIES,
        "limitations": LIMITATIONS,
        "prompt_template": REQUEST_CONTRACT,
        "mcp_endpoint": "/api/mcp/transport",
        "defaults": normalized_config({}, {}),
    }


@router.get("/tasks")
def list_tasks():
    return {"tasks": invoke(get_service().tasks)}


@router.post("/tasks", status_code=201)
def create_task(body: CreateTask):
    return {"task": invoke(get_service().create, body.model_dump())}


@router.get("/tasks/{task_id}")
def get_task(task_id: str):
    return {"task": invoke(get_service().task, task_id)}


@router.patch("/tasks/{task_id}")
def update_task(task_id: str, body: UpdateTask):
    return {"task": invoke(get_service().update, task_id, body.model_dump(exclude_none=True))}


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    return invoke(get_service().delete, task_id)


@router.post("/tasks/{task_id}/start")
def start_capture(task_id: str, request: Request):
    task = invoke(get_service().start, task_id, connection_host=request.url.hostname)
    return {"task": task, "session": task.get("session")}


@router.get("/tasks/{task_id}/connection")
def capture_connection(task_id: str):
    return JSONResponse(
        invoke(get_service().connection, task_id),
        headers={"Cache-Control": "no-store", "Vary": "Origin, Authorization, X-MCP-Token"},
    )


@router.post("/tasks/{task_id}/stop")
def stop_capture(task_id: str):
    return {"task": invoke(get_service().stop, task_id)}


@router.post("/tasks/{task_id}/end")
def end_task(task_id: str):
    return {"task": invoke(get_service().end, task_id)}


@router.get("/tasks/{task_id}/flows")
def list_flows(task_id: str, q: str = "", kind: str = "", source: str = "", after: int = 0, limit: int = 50):
    service = get_service()
    invoke(service.refresh, task_id)
    page = invoke(service.store.flows, task_id, q=q, kind=kind, source=source, after=after, limit=limit)
    page["flows"] = [
        {k: v for k, v in public_flow(flow).items() if k not in {"request", "response"}}
        for flow in page["flows"]
    ]
    return page


@router.get("/tasks/{task_id}/flows/{flow_id}")
def get_flow(task_id: str, flow_id: str, reveal: bool = False):
    return {"flow": public_flow(invoke(get_service().store.get_flow, task_id, flow_id), reveal=reveal)}


@router.post("/tasks/{task_id}/flows/{flow_id}/replay")
def replay_flow(task_id: str, flow_id: str, body: ReplayBody, request: Request):
    flow = invoke(
        get_service().replay,
        task_id,
        flow_id,
        body.modifications,
        key=request.headers.get("idempotency-key", ""),
    )
    return {"flow": public_flow(flow)}


@router.get("/tasks/{task_id}/endpoints")
def list_endpoints(task_id: str):
    service = get_service()
    invoke(service.refresh, task_id)
    return {"endpoints": invoke(service.store.endpoints, task_id)}


@router.get("/tasks/{task_id}/tests")
def list_tests(task_id: str):
    service = get_service()
    invoke(service.store.get_task, task_id)
    return {"tests": [service.public_test(job) for job in service.store.records("jobs", task_id)]}


@router.post("/tasks/{task_id}/tests", status_code=202)
def create_test(task_id: str, body: TestBody, request: Request):
    service = get_service()
    test = invoke(
        service.start_test,
        task_id,
        body.model_dump(exclude_none=True),
        key=request.headers.get("idempotency-key", ""),
    )
    return {"test": service.public_test(test)}


@router.get("/tasks/{task_id}/tests/{test_id}")
def get_test(task_id: str, test_id: str):
    service = get_service()
    return {"test": service.public_test(invoke(service.store.record, "jobs", task_id, test_id), detail=True)}


@router.post("/tasks/{task_id}/tests/{test_id}/cancel")
def cancel_test(task_id: str, test_id: str):
    service = get_service()
    return {"test": service.public_test(invoke(service.cancel_test, task_id, test_id))}


@router.get("/tasks/{task_id}/reports")
def list_reports(task_id: str):
    service = get_service()
    invoke(service.store.get_task, task_id)
    return {
        "reports": [
            {k: v for k, v in report.items() if k not in {"sources", "markdown"}}
            for report in service.store.records("reports", task_id)
        ]
    }


@router.post("/tasks/{task_id}/reports", status_code=201)
def create_report(task_id: str, request: Request):
    return {"report": invoke(get_service().report, task_id, key=request.headers.get("idempotency-key", ""))}


@router.get("/tasks/{task_id}/reports/{report_id}")
def get_report(task_id: str, report_id: str):
    return {"report": invoke(get_service().store.record, "reports", task_id, report_id)}


@router.get("/tasks/{task_id}/ca")
def public_ca(task_id: str):
    path = invoke(get_service().ca, task_id)
    return FileResponse(path, filename="strixops-mcp-ca.pem", media_type="application/x-pem-file")


@router.get("/ca/info")
def shared_ca_info():
    return invoke(get_service().ca_info)


@router.get("/ca")
def shared_ca():
    path = invoke(get_service().shared_ca)
    return FileResponse(
        path,
        filename="strixops-mcp-ca.pem",
        media_type="application/x-pem-file",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/tasks/{task_id}/events")
async def stream_events(task_id: str, request: Request, after: int = 0):
    service = get_service()
    invoke(service.store.get_task, task_id)
    try:
        after = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError as exc:
        raise HTTPException(400, "Invalid event cursor") from exc

    async def events():
        cursor = after
        while not await request.is_disconnected():
            try:
                await asyncio.to_thread(service.store.get_task, task_id)
            except KeyError:
                yield (
                    "event: deleted\ndata: "
                    + json.dumps({"type": "task.deleted", "task_id": task_id})
                    + "\n\n"
                )
                return
            rows = await asyncio.to_thread(service.store.events, task_id, cursor)
            for row in rows:
                cursor = row["id"]
                yield f"id: {cursor}\ndata: {json.dumps(row)}\n\n"
            if not rows:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
