"""Additive Console routes with bounded bodies and safe credential validation."""

from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from .client import FIELDS
from .errors import FofaError
from .service import FofaService, close_existing, get_service
from .settings import public_settings, read_settings, save_settings
from .store import FILTER_FIELDS, filters, integer


class SafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                # Console remains the deployment access boundary. Match the
                # existing MCP same-origin/trusted-local-rewrite convention.
                from strixops.traffic.api import origin_error

                if origin_error(request.scope):
                    raise FofaError("origin_rejected", 403)
                response = await original(request)
            except FofaError as exc:
                response = JSONResponse(exc.public(), status_code=exc.status)
            response.headers["Cache-Control"] = "no-store"
            return response

        return handler


async def body(request: Request, allowed: set[str]) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 32768:
            raise FofaError("invalid_request")
        raw.extend(chunk)
    try:
        result = json.loads(raw or b"{}")
    except (ValueError, RecursionError):
        raise FofaError("invalid_request") from None
    if not isinstance(result, dict) or set(result) - allowed:
        raise FofaError("invalid_request")
    return result


def query_options(request: Request, *, export: bool = False) -> dict:
    allowed = FILTER_FIELDS | {"sort", "order", "limit", "offset"} | ({"format"} if export else set())
    pairs = list(request.query_params.multi_items())
    if any(key not in allowed for key, _ in pairs) or len(pairs) != len(set(key for key, _ in pairs)):
        raise FofaError("invalid_request")
    selected = filters({key: value for key, value in pairs if key in FILTER_FIELDS})
    return {
        "selected_filters": selected,
        "sort": request.query_params.get("sort", "position"),
        "direction": request.query_params.get("order", "asc"),
        "limit": 10000 if export else integer(request.query_params.get("limit"), 50, 1, 100),
        "offset": 0 if export else integer(request.query_params.get("offset"), 0, 0, 10000),
    }


def _csv_cell(value: object) -> object:
    if isinstance(value, str) and (
        value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))
    ):
        return "'" + value
    return value


def create_router(*, root: Path | None = None, service: FofaService | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/fofa", route_class=SafeRoute)

    def current() -> FofaService:
        return service or get_service(root)

    @router.get("/settings")
    async def settings_get():
        return public_settings(read_settings(current().root))

    @router.put("/settings")
    async def settings_put(request: Request):
        data = await body(request, {"enabled", "email", "key", "clear_key"})
        return {"ok": True, **save_settings(current().root, data)}

    @router.post("/settings/test")
    async def settings_test(request: Request):
        await body(request, set())
        item = current()
        return await item.client.test(read_settings(item.root))

    @router.post("/searches")
    async def searches_post(request: Request):
        search = await current().start(await body(request, {"query", "max_results"}))
        return JSONResponse({"search": search}, status_code=202)

    @router.get("/searches")
    async def searches_get(request: Request):
        item = current()
        item.recover_if_idle()
        if set(request.query_params) - {"limit", "offset"}:
            raise FofaError("invalid_request")
        return item.store.searches(
            integer(request.query_params.get("limit"), 20, 1, 100),
            integer(request.query_params.get("offset"), 0, 0, 1000000),
        )

    @router.get("/searches/{search_id}")
    async def search_get(search_id: str):
        current().recover_if_idle()
        return {"search": current().store.search(search_id)}

    @router.delete("/searches/{search_id}")
    async def search_delete(search_id: str):
        await current().delete(search_id)
        return {"ok": True}

    @router.get("/searches/{search_id}/results")
    async def results_get(search_id: str, request: Request):
        return current().store.results(search_id, **query_options(request))

    @router.get("/searches/{search_id}/export")
    async def export_get(search_id: str, request: Request):
        format_name = request.query_params.get("format", "csv")
        if format_name not in {"csv", "json"}:
            raise FofaError("invalid_request")
        rows = current().store.results(search_id, **query_options(request, export=True))["results"]
        if format_name == "json":
            content = json.dumps({"search_id": search_id, "results": rows}, ensure_ascii=False).encode()
            media = "application/json"
        else:
            stream = io.StringIO(newline="")
            names = ("result_id", *FIELDS, "web_target", "selectable", "selection_reason")
            writer = csv.writer(stream)
            writer.writerow(names)
            writer.writerows([_csv_cell(row.get(name)) for name in names] for row in rows)
            content = stream.getvalue().encode("utf-8-sig")
            media = "text/csv"
        return Response(
            content,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="fofa-{search_id}.{format_name}"'},
        )

    @router.post("/searches/{search_id}/draft")
    async def draft_post(search_id: str, request: Request):
        return current().store.create_draft(
            search_id, await body(request, {"result_ids", "all_matching", "filters"})
        )

    @router.get("/drafts/{draft_id}")
    async def draft_get(draft_id: str):
        return current().store.draft(draft_id)

    return router


def install(app: FastAPI, *, root: Path | None = None, service: FofaService | None = None):
    app.include_router(create_router(root=root, service=service))
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application) as state:
            try:
                yield state
            finally:
                if service is not None:
                    await service.close()
                else:
                    await close_existing(root)

    app.router.lifespan_context = lifespan
