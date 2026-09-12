"""Single-account HTTP boundary; every Console API passes through this layer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from starlette._utils import get_route_path
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response

from strixops.console.auth_store import AuthError, get_store

COOKIE = "strixops_session"
SECURE_COOKIE = "__Host-strixops_session"
AUTH_PREFIX = "/api/auth/"
AUTH_PATHS = {AUTH_PREFIX + name for name in ("session", "login", "logout", "password", "account")}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
BASE_CSP = (
    "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
    "form-action 'self'; img-src 'self' data: blob:; font-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; connect-src 'self'; worker-src 'self' blob:; "
)
router = APIRouter(prefix="/api/auth", tags=["account"])


def error_response(code: str, status: int, retry_after: int | None = None) -> JSONResponse:
    # No request body, exception text, database paths or supplied passwords in responses.
    response = JSONResponse({"detail": {"code": code, "message": code}}, status_code=status)
    if retry_after is not None:
        response.headers["Retry-After"] = str(max(1, int(retry_after)))
    return response


def cookie_name(scope: dict) -> str:
    return SECURE_COOKIE if scope.get("scheme") == "https" else COOKIE


def session_token(request: Request) -> str:
    return request.cookies.get(cookie_name(request.scope), "")


def origin_allowed(scope: dict) -> bool:
    """Only a browser's exact origin; never trust arbitrary forwarded headers."""
    headers = Headers(scope=scope)
    if headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = headers.get("origin")
    if not origin:
        return True  # Unsafe requests additionally require an unguessable CSRF header.
    try:
        source = urlsplit(origin)
        destination = urlsplit(f"{scope.get('scheme', 'http')}://{headers.get('host', '')}")
        if (
            source.scheme not in {"http", "https"}
            or source.username is not None
            or source.password is not None
            or source.path
            or source.query
            or source.fragment
            or not source.hostname
        ):
            return False

        def authority(value):
            return value.scheme, value.hostname, value.port or (443 if value.scheme == "https" else 80)

        if authority(source) == authority(destination):
            return True
        # Development rewrites may replace Host. Any exception is explicit and
        # limited to a local upstream; forwarded-host alone grants no trust.
        peer = (scope.get("client") or ("", 0))[0]
        allowed = {
            item.strip().rstrip("/")
            for item in os.environ.get("STRIXOPS_AUTH_TRUSTED_ORIGINS", "").split(",")
            if item.strip()
        }
        return peer in {"127.0.0.1", "::1"} and origin in allowed
    except ValueError:
        return False


def machine_token_allowed(scope: dict) -> bool:
    """An MCP token is valid only for the protocol endpoint, never the Console API."""
    if get_route_path(scope) != "/api/mcp/transport":
        return False
    headers = Headers(scope=scope)
    authorization = headers.get("authorization", "")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else headers.get("x-mcp-token", "")
    if not supplied or len(supplied) > 4096:
        return False
    # Old automatic browser tokens were issued before Console login existed.
    # They must never become a persistent bypass around the new account boundary.
    configured = os.environ.get("STRIXOPS_MCP_TOKEN", "")
    return bool(configured) and hmac.compare_digest(supplied.encode(), configured.encode())


async def payload(request: Request, fields: set[str]) -> dict | None:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return None
    body = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                if len(body) + len(chunk) > 4096:
                    return None
                body.extend(chunk)
    except TimeoutError:
        return None

    def unique_object(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate field")
        return value

    try:
        value = json.loads(body, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(value, dict) or set(value) != fields:
        return None
    if any(not isinstance(value[key], str) or len(value[key]) > 512 for key in fields):
        return None
    return value


def client_ip(request: Request) -> str:
    # Uvicorn resolves proxy headers only from explicitly trusted proxy peers.
    # No direct use of X-Forwarded-For / X-Real-IP / Forwarded supplied by a client.
    return request.client.host if request.client else "unknown"


def authenticated_response(result: dict, request: Request) -> JSONResponse:
    response = JSONResponse({"authenticated": True, **result["session"], "csrf_token": result["csrf_token"]})
    secure = request.scope.get("scheme") == "https"
    if secure:
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
    response.set_cookie(
        cookie_name(request.scope),
        result["token"],
        max_age=12 * 3600,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/session")
async def current_session(request: Request):
    session = request.scope.get("state", {}).get("strixops_session")
    return {"authenticated": bool(session), **(session or {})}


@router.post("/login")
async def login(request: Request):
    if request.headers.get("x-strixops-request") != "1":
        return error_response("origin_rejected", 403)
    data = await payload(request, {"username", "password"})
    if data is None:
        return error_response("invalid_request", 400)
    try:
        store = await run_in_threadpool(get_store)
        result = await run_in_threadpool(store.login, data["username"], data["password"], client_ip(request))
        previous = session_token(request)
        if previous:
            await run_in_threadpool(store.logout, previous)
        return authenticated_response(result, request)
    except AuthError as exc:
        return error_response(exc.code, exc.status, exc.retry_after)


@router.get("/account")
async def account():
    try:
        store = await run_in_threadpool(get_store)
        data = await run_in_threadpool(store.account)
        data["login_history"] = await run_in_threadpool(store.history)
        return data
    except AuthError as exc:
        return error_response(exc.code, exc.status, exc.retry_after)


@router.post("/password")
async def change_password(request: Request):
    data = await payload(request, {"current_password", "new_password"})
    if data is None:
        return error_response("invalid_request", 400)
    try:
        store = await run_in_threadpool(get_store)
        result = await run_in_threadpool(
            store.change_password,
            session_token(request),
            data["current_password"],
            data["new_password"],
            client_ip(request),
        )
        return authenticated_response(result, request)
    except AuthError as exc:
        return error_response(exc.code, exc.status, exc.retry_after)


@router.post("/logout")
async def logout(request: Request):
    try:
        store = await run_in_threadpool(get_store)
        await run_in_threadpool(store.logout, session_token(request))
    except AuthError as exc:
        return error_response(exc.code, exc.status, exc.retry_after)
    response = Response(status_code=204)
    response.delete_cookie(
        cookie_name(request.scope),
        path="/",
        secure=request.scope.get("scheme") == "https",
        httponly=True,
        samesite="strict",
    )
    return response


@lru_cache(maxsize=32)
def static_csp(path: str, mtime_ns: int, size: int) -> str:
    """Authorize exactly the inline hydration/theme scripts shipped in this HTML."""
    del mtime_ns, size  # Cache key changes whenever the static build changes.
    content = Path(path).read_bytes()
    scripts = re.findall(rb"<script(?:\s[^>]*)?>(.*?)</script\s*>", content, flags=re.DOTALL | re.IGNORECASE)
    hashes = sorted(
        {
            "'sha256-" + base64.b64encode(hashlib.sha256(script).digest()).decode() + "'"
            for script in scripts
            if script
        }
    )
    return BASE_CSP + "script-src 'self' " + " ".join(hashes) + ";"


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = get_route_path(scope)
        protected = (
            path == "/api"
            or path.startswith("/api/")
            or path in {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
        )
        request = Request(scope)
        session = None
        token = session_token(request)
        store = None
        ended = False
        streaming = False
        checked_at = 0.0

        async def secure_send(message):
            nonlocal streaming, ended, checked_at
            if ended:
                return
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                headers.setdefault("Content-Security-Policy", BASE_CSP + "script-src 'self';")
                if protected or "text/html" in headers.get("content-type", ""):
                    headers["Cache-Control"] = "no-store"
                streaming = "text/event-stream" in headers.get("content-type", "")
            elif message["type"] == "http.response.body" and streaming and session and store:
                now = asyncio.get_running_loop().time()
                if now - checked_at >= 1:
                    checked_at = now
                    try:
                        live = await run_in_threadpool(store.authenticate, token, touch=False)
                    except AuthError:
                        live = None
                    if not live or live["must_change_password"]:
                        ended = True
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                        raise SessionStreamEnded
            await send(message)

        if protected and not origin_allowed(scope):
            await error_response("origin_rejected", 403)(scope, receive, secure_send)
            return
        if protected and path != "/api/health":
            try:
                machine = await run_in_threadpool(machine_token_allowed, scope)
                if machine:
                    scope.setdefault("state", {})["strixops_mcp_token_authenticated"] = True
                else:
                    store = await run_in_threadpool(get_store)
                    session = await run_in_threadpool(store.authenticate, token) if token else None
                    scope.setdefault("state", {})["strixops_session"] = session
                    if path not in {AUTH_PREFIX + "session", AUTH_PREFIX + "login"}:
                        if not session:
                            await error_response("authentication_required", 401)(scope, receive, secure_send)
                            return
                        if session["must_change_password"] and path not in AUTH_PATHS:
                            await error_response("password_change_required", 403)(scope, receive, secure_send)
                            return
                        if scope["method"] not in SAFE_METHODS and not await run_in_threadpool(
                            store.validate_csrf, token, request.headers.get("x-csrf-token", "")
                        ):
                            await error_response("csrf_failed", 403)(scope, receive, secure_send)
                            return
                        scope["state"]["strixops_authenticated"] = not session["must_change_password"]
            except AuthError as exc:
                await error_response(exc.code, exc.status, exc.retry_after)(scope, receive, secure_send)
                return
        try:
            await self.app(scope, receive, secure_send)
        except SessionStreamEnded:
            return


class SessionStreamEnded(Exception):
    """Stop an already-open SSE response after its session expires or is revoked."""
