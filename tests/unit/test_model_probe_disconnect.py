"""A canceled console model test must also cancel its provider request."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request

from strixops.console import server


@pytest.fixture(autouse=True)
def fast_disconnect_poll(monkeypatch):
    monkeypatch.setattr(server, "MODEL_PROBE_DISCONNECT_POLL_SECONDS", 0.001)


@pytest.mark.asyncio
async def test_disconnected_http_request_cancels_and_awaits_probe(monkeypatch):
    cleanup_done = asyncio.Event()
    canceled = False

    async def probe(**kwargs):
        nonlocal canceled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            canceled = True
            raise
        finally:
            # Cleanup may itself suspend while the provider client closes.
            await asyncio.sleep(0)
            cleanup_done.set()

    monkeypatch.setattr(server.model_probe, "test_model", probe)
    payload = json.dumps({"model": "deployment-alias"}).encode()
    body_delivered = False
    response_messages = []

    async def receive():
        nonlocal body_delivered
        if not body_delivered:
            body_delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        response_messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/settings/test-model",
        "raw_path": b"/api/settings/test-model",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 1),
    }
    await asyncio.wait_for(server.app(scope, receive, send), timeout=1)

    assert canceled
    assert cleanup_done.is_set()
    response = next(message for message in response_messages if message["type"] == "http.response.start")
    assert response["status"] == 499


@pytest.mark.asyncio
async def test_canceling_handler_also_awaits_probe_cleanup(monkeypatch):
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def probe(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    monkeypatch.setattr(server.model_probe, "test_model", probe)
    request = Request({"type": "http"})
    monkeypatch.setattr(request, "is_disconnected", AsyncMock(return_value=False))
    handler = asyncio.create_task(server.test_provider_model(server.ModelProbeBody(), request))
    await started.wait()
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    assert cleaned.is_set()


@pytest.mark.parametrize("error", [None, HTTPException(status_code=502, detail={"code": "upstream_auth"})])
@pytest.mark.asyncio
async def test_completed_probe_preserves_result_or_original_error(monkeypatch, error):
    expected = {"ok": True, "api_mode": "responses"}
    probe = AsyncMock(return_value=expected, side_effect=error)
    monkeypatch.setattr(server.model_probe, "test_model", probe)
    request = Request({"type": "http"})
    monkeypatch.setattr(request, "is_disconnected", AsyncMock(return_value=False))
    if error is None:
        result = await server.test_provider_model(server.ModelProbeBody(), request)
        assert result == expected
    else:
        with pytest.raises(HTTPException) as caught:
            await server.test_provider_model(server.ModelProbeBody(), request)
        assert caught.value is error
    probe.assert_awaited_once()
