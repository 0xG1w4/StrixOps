"""Official MCP SDK adapter; task and proxy lifetimes never depend on clients."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import wraps

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
from starlette.routing import Route

from strixops.traffic.api import access_error
from strixops.traffic.security import public_flow
from strixops.traffic.service import get_service


def build_server() -> FastMCP:
    # The common API boundary verifies origin, local hosts/peers and optional tokens.
    server = FastMCP(
        "StrixOps MCP Tasks",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/api/mcp/transport",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        instructions="Manage independent traffic tasks. Capture does not authorize active testing. "
        "Use selected persisted request IDs; test and report tools return durable job/version IDs.",
    )

    def tool(*, read_only=False, idempotent=False, destructive=False, open_world=True):
        def register(function):
            @wraps(function)
            async def in_worker(*args, **kwargs):
                # Docker and SQLite operations must not block the shared Console event loop.
                return await asyncio.to_thread(function, *args, **kwargs)

            return server.tool(
                annotations=ToolAnnotations(
                    readOnlyHint=read_only,
                    idempotentHint=idempotent,
                    destructiveHint=destructive,
                    openWorldHint=open_world,
                )
            )(in_worker)

        return register

    @tool(open_world=False)
    def traffic_create_task(
        name: str, allow_hosts: list[str], exclude_hosts: list[str] | None = None
    ) -> dict:
        """Create an independent task with an explicit website capture allowlist; does not start a proxy."""
        return get_service().create(
            {"name": name, "allow_hosts": allow_hosts, "exclude_hosts": exclude_hosts or []}
        )

    @tool(read_only=True, open_world=False)
    def traffic_list_tasks() -> dict:
        """List saved MCP tasks and their capture status."""
        return {"tasks": get_service().tasks()}

    @tool(idempotent=True)
    def traffic_start_capture(task_id: str) -> dict:
        """Start this task's owned proxy container and return its configured listener address."""
        return get_service().start(task_id)

    @tool(idempotent=True)
    def traffic_stop_capture(task_id: str) -> dict:
        """Stop this task's proxy while preserving captured evidence and test results."""
        return get_service().stop(task_id)

    @tool(idempotent=True)
    def traffic_end_task(task_id: str) -> dict:
        """End capture and cancel this task's tests; preserve all saved records."""
        return get_service().end(task_id)

    @tool(idempotent=True, destructive=True, open_world=False)
    def traffic_delete_task(task_id: str) -> dict:
        """Stop this task's proxy and tests, then permanently delete its requests, results and reports."""
        return get_service().delete(task_id)

    @tool(read_only=True, open_world=False)
    def traffic_get_status(task_id: str) -> dict:
        """Read capture status, listener information and saved record counts."""
        return get_service().task(task_id)

    @tool(read_only=True, open_world=False)
    def traffic_list_requests(
        task_id: str, query: str = "", kind: str = "", after: int = 0, limit: int = 50
    ) -> dict:
        """Read bounded, redacted request summaries. Use next_cursor to request another page."""
        service = get_service()
        service.refresh(task_id)
        page = service.store.flows(task_id, q=query, kind=kind, after=after, limit=limit)
        page["flows"] = [
            {k: v for k, v in public_flow(flow).items() if k not in {"request", "response"}}
            for flow in page["flows"]
        ]
        return page

    @tool(read_only=True, open_world=False)
    def traffic_get_request(task_id: str, flow_id: str) -> dict:
        """Read one captured request/response with credentials redacted."""
        return public_flow(get_service().store.get_flow(task_id, flow_id))

    @tool(read_only=True, open_world=False)
    def traffic_list_endpoints(task_id: str) -> dict:
        """List observed endpoints; normalized path groups are inferences, not exhaustive discovery."""
        service = get_service()
        service.refresh(task_id)
        return {"endpoints": service.store.endpoints(task_id)}

    @tool(destructive=True)
    def traffic_replay_request(
        task_id: str, flow_id: str, modifications: dict | None = None, idempotency_key: str = ""
    ) -> dict:
        """Send a selected request to its allowed origin; may change target data."""
        return public_flow(get_service().replay(task_id, flow_id, modifications, key=idempotency_key))

    @tool(destructive=True)
    def traffic_start_test(
        task_id: str,
        flow_ids: list[str],
        skills: list[str] | None = None,
        instruction: str = "",
        profile_id: str = "",
        max_requests: int | None = None,
        max_seconds: int | None = None,
        idempotency_key: str = "",
    ) -> dict:
        """Start a bounded Agent test for selected requests and return immediately with its job ID."""
        service = get_service()
        body = {"flow_ids": flow_ids}
        if max_requests is not None:
            body["max_requests"] = max_requests
        if max_seconds is not None:
            body["max_seconds"] = max_seconds
        if skills is not None:
            body["skills"] = skills
        if instruction:
            body["instruction"] = instruction
        if profile_id:
            body["profile_id"] = profile_id
        return service.public_test(service.start_test(task_id, body, key=idempotency_key))

    @tool(read_only=True, open_world=False)
    def traffic_get_test(task_id: str, test_id: str) -> dict:
        """Read test progress, results and prompt/skill manifest."""
        service = get_service()
        return service.public_test(service.store.record("jobs", task_id, test_id), detail=True)

    @tool(idempotent=True)
    def traffic_cancel_test(task_id: str, test_id: str) -> dict:
        """Cancel this test without stopping capture."""
        service = get_service()
        return service.public_test(service.cancel_test(task_id, test_id))

    @tool(open_world=False)
    def traffic_generate_report(task_id: str, idempotency_key: str = "") -> dict:
        """Create an immutable report version from observed endpoints and saved test evidence."""
        return get_service().report(task_id, key=idempotency_key)

    @tool(read_only=True, open_world=False)
    def traffic_get_report(task_id: str, report_id: str) -> dict:
        """Read a saved report version; creating a new version never changes existing reports."""
        return get_service().store.record("reports", task_id, report_id)

    @tool(read_only=True, open_world=False)
    def traffic_get_catalog() -> dict:
        """Read compatible Web skills, model profiles and Request mode defaults."""
        from strixops.traffic.api import catalog

        return catalog()

    @tool(read_only=True, open_world=False)
    def traffic_get_ca_info() -> dict:
        """Read the shared CA fingerprint and validity; the private signing key is never returned."""
        return {**get_service().ca_info(), "download_path": "/api/mcp/ca"}

    @tool(idempotent=True, open_world=False)
    def traffic_update_task(
        task_id: str,
        allow_hosts: list[str] | None = None,
        exclude_hosts: list[str] | None = None,
        agent_config: dict | None = None,
    ) -> dict:
        """Update this task's website rules or Request Agent defaults; preserves historical evidence."""
        changes = {
            key: value
            for key, value in {
                "allow_hosts": allow_hosts,
                "exclude_hosts": exclude_hosts,
                "agent_config": agent_config,
            }.items()
            if value is not None
        }
        return get_service().update(task_id, changes)

    return server


def install(app) -> None:
    from strixops.traffic.api import access_router, router

    app.include_router(access_router)
    app.include_router(router)
    active = {}
    prior = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with prior(application):
            server = build_server()
            active["app"] = server.streamable_http_app()
            async with server.session_manager.run():
                try:
                    yield
                finally:
                    active.clear()

    app.router.lifespan_context = lifespan

    class Transport:
        async def __call__(self, scope, receive, send):
            if error := access_error(scope):
                await JSONResponse({"detail": error}, status_code=403)(scope, receive, send)
                return
            if "app" not in active:
                await JSONResponse({"detail": "MCP transport is starting"}, status_code=503)(
                    scope, receive, send
                )
                return
            await active["app"](scope, receive, send)

    app.router.routes.append(Route("/api/mcp/transport", Transport(), methods=["POST", "GET", "DELETE"]))
