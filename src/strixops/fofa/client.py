"""Bounded FOFA HTTP adapter. No probing of discovered hosts occurs here."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Callable

import httpx

from .errors import FofaError

ENDPOINT = "https://fofa.info/api/v1/"
FIELDS = (
    "host",
    "ip",
    "port",
    "protocol",
    "domain",
    "title",
    "country",
    "region",
    "city",
    "server",
    "lastupdatetime",
)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT = 30.0


def _error_code(data: dict, status: int) -> str | None:
    message = data.get("errmsg", "")
    message = message.lower()[:4096] if isinstance(message, str) else ""
    # Provider error messages are classified locally and are never returned or logged.
    if status in {401, 403} or (
        data.get("error") is True
        and any(
            word in message for word in ("api key", "apikey", "invalid key", "账号", "账户", "认证", "权限")
        )
    ):
        return "unauthorized"
    if status == 402 or (
        data.get("error") is True
        and any(
            word in message
            for word in (
                "quota",
                "credit",
                "balance",
                "limit exceeded",
                "insufficient",
                "积分",
                "余额",
                "额度",
                "数量",
            )
        )
    ):
        return "quota_exceeded"
    if status == 429 or (data.get("error") is True and re.match(r"\[45012\]", message)):
        return "rate_limited"
    if status != 200 or data.get("error") is True:
        return "provider_error"
    return None


def _integer(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


class FofaClient:
    def __init__(self, *, transport_factory: Callable | None = None, timeout: float = REQUEST_TIMEOUT):
        self.transport_factory = transport_factory or (
            lambda: httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        )
        self.timeout = timeout

    async def fetch(self, endpoint: str, settings: dict, params: dict | None = None) -> dict:
        if not settings["enabled"]:
            raise FofaError("disabled")
        if not settings["key"]:
            raise FofaError("not_configured")
        if endpoint not in {"info/my", "search/all"}:
            raise FofaError("invalid_request")
        query = {"key": settings["key"], **(params or {})}
        if settings["email"]:
            query["email"] = settings["email"]
        try:
            async with asyncio.timeout(self.timeout):
                for attempt in range(2):
                    # Use the public transport interface directly: AsyncClient logs the
                    # complete request URL, including FOFA's query-string credential.
                    request = httpx.Request(
                        "GET",
                        ENDPOINT + endpoint,
                        params=query,
                        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                        extensions={
                            "timeout": {name: self.timeout for name in ("connect", "read", "write", "pool")}
                        },
                    )
                    async with self.transport_factory() as transport:
                        response = await transport.handle_async_request(request)
                        try:
                            if response.status_code in {401, 403, 402}:
                                raise FofaError(
                                    _error_code({}, response.status_code) or "provider_error", 502
                                )
                            encoding = response.headers.get("content-encoding", "identity").strip().lower()
                            if encoding not in {"", "identity"}:
                                raise FofaError("invalid_response", 502)
                            content = bytearray()
                            if response.is_stream_consumed:
                                # Some injected/public transports return a buffered response.
                                if len(response.content) > MAX_RESPONSE_BYTES:
                                    raise FofaError("response_too_large", 502)
                                content.extend(response.content)
                            else:
                                async for chunk in response.aiter_raw():
                                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                                        raise FofaError("response_too_large", 502)
                                    content.extend(chunk)
                            try:
                                data = json.loads(
                                    content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
                                )
                            except (ValueError, RecursionError):
                                if response.status_code == 429:
                                    data = {}
                                else:
                                    raise FofaError("invalid_response", 502) from None
                            if not isinstance(data, dict):
                                raise FofaError("invalid_response", 502)
                            code = _error_code(data, response.status_code)
                            if code == "rate_limited" and attempt == 0:
                                raw = response.headers.get("retry-after", "1")
                                delay = min(int(raw), 5) if raw.isdigit() and len(raw) <= 6 else 1
                            elif code:
                                raise FofaError(code, 502)
                            elif data.get("error") is not False:
                                raise FofaError("invalid_response", 502)
                            else:
                                return data
                        finally:
                            await response.aclose()
                    await asyncio.sleep(delay)
        except (TimeoutError, httpx.TimeoutException):
            raise FofaError("timeout", 504) from None
        except (httpx.HTTPError, OSError, ValueError):
            raise FofaError("provider_error", 502) from None
        raise FofaError("provider_error", 502)

    async def test(self, settings: dict) -> dict:
        started = time.monotonic()
        try:
            data = await self.fetch("info/my", settings)
            if type(data.get("isvip")) is not bool:
                raise FofaError("invalid_response", 502)
            result = {
                "success": True,
                "code": "ok",
                "account": {
                    "is_vip": data["isvip"],
                    "vip_level": _integer(data.get("vip_level")),
                    "remaining_queries": _integer(data.get("remain_api_query")),
                    "remaining_data": _integer(data.get("remain_api_data")),
                },
            }
        except FofaError as exc:
            result = {"success": False, "code": exc.code, "account": None}
        return {**result, "duration_seconds": round(time.monotonic() - started, 3)}

    async def page(self, query: str, page: int, size: int, settings: dict) -> tuple[list[list], int | None]:
        data = await self.fetch(
            "search/all",
            settings,
            {
                "qbase64": base64.b64encode(query.encode()).decode(),
                "page": str(page),
                "size": str(size),
                "fields": ",".join(FIELDS),
                "full": "false",
            },
        )
        rows = data.get("results")
        if not isinstance(rows, list) or len(rows) > size:
            raise FofaError("invalid_response", 502)
        for row in rows:
            if (
                not isinstance(row, list)
                or len(row) != len(FIELDS)
                or any(type(cell) not in {str, int} or len(str(cell)) > 8192 for cell in row)
            ):
                raise FofaError("invalid_response", 502)
        return rows, _integer(data.get("size"))
