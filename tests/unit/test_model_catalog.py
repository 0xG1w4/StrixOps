"""Model discovery reads draft endpoints and protects saved credentials without live HTTP."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, Mock

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

from strixops.console import model_catalog, server, settings_store


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("STRIXOPS_CONSOLE_CONFIG", str(path))
    return path


@pytest.fixture(autouse=True)
def http(monkeypatch):
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = Mock(status_code=200, json=Mock(return_value={"data": [{"id": "model-a"}]}))
    monkeypatch.setattr(model_catalog.requests, "Session", Mock(return_value=session))
    return session


def _discover(**overrides):
    body = {
        "route_type": "custom",
        "llm_api_base": "https://provider.example/v1",
        "llm_api_key": "test-explicit-key",
    }
    body.update(overrides)
    return model_catalog.discover_models(**body)


def _save_profile(**overrides):
    profile = {
        "id": "saved-profile",
        "route_type": "custom",
        "llm_api_base": "https://provider.example/v1/",
        "llm_api_key": "test-saved-secret",
    }
    profile.update(overrides)
    data = settings_store.load_settings()
    data["profiles"] = [profile]
    settings_store.save_settings(data)
    return profile


def test_endpoint_fetches_sorted_unique_model_ids_without_saving(http, isolated_settings):
    http.get.return_value.json.return_value = {
        "data": [
            {"id": "z-ai/glm-model", "name": "GLM Model"},
            {"id": "openrouter/openai/model:free", "name": "Friendly Name"},
            {"id": "a-local-model"},
            {"id": "a-local-model", "name": "Duplicate"},
        ]
    }
    with TestClient(server.app) as client:
        response = client.post(
            "/api/settings/models",
            json={
                "route_type": "custom",
                "llm_api_base": "HTTPS://Provider.Example:443/v1///",
                "llm_api_key": "test-explicit-key",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "models": [
            {"id": "a-local-model", "name": "a-local-model"},
            {"id": "openrouter/openai/model:free", "name": "Friendly Name"},
            {"id": "z-ai/glm-model", "name": "GLM Model"},
        ],
        "count": 3,
    }
    http.get.assert_called_once_with(
        "https://provider.example/v1/models",
        headers={"Authorization": "Bearer test-explicit-key", "Accept": "application/json"},
        timeout=12,
        allow_redirects=False,
    )
    assert http.trust_env is False
    assert not isolated_settings.exists()
    assert "test-explicit-key" not in response.text


@pytest.mark.parametrize("base", ["http://localhost:8000/v1/", "http://[::1]:8000/v1/"])
def test_local_custom_providers_are_supported(http, base):
    assert _discover(llm_api_base=base)["count"] == 1
    assert http.get.call_args.args[0] == base.rstrip("/") + "/models"


def test_openrouter_pins_endpoint_regardless_of_draft_base(http):
    _discover(route_type="openrouter", llm_api_base="https://unrelated.example/v1")
    assert http.get.call_args.args[0] == "https://openrouter.ai/api/v1/models"


@pytest.mark.parametrize("key", ["", "•••cret"])
def test_edit_reuses_key_only_at_same_normalized_endpoint(http, isolated_settings, key):
    _save_profile()
    before = isolated_settings.read_bytes()

    result = _discover(
        profile_id="saved-profile", llm_api_base="HTTPS://PROVIDER.EXAMPLE:443/v1", llm_api_key=key
    )

    assert result["count"] == 1
    assert http.get.call_args.kwargs["headers"]["Authorization"] == "Bearer test-saved-secret"
    assert isolated_settings.read_bytes() == before
    assert "test-saved-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "change",
    [
        {"llm_api_base": "https://different.example/v1"},
        {"llm_api_base": "https://provider.example/v2"},
        {"llm_api_base": "http://provider.example/v1"},
        {"route_type": "openrouter"},
    ],
)
def test_changed_destination_never_receives_stored_key(http, change):
    _save_profile()
    with pytest.raises(HTTPException) as error:
        _discover(profile_id="saved-profile", llm_api_key="•••cret", **change)

    assert error.value.detail["code"] == "endpoint_changed"
    http.get.assert_not_called()


def test_explicit_key_allows_new_endpoint_without_changing_saved_profile(http, isolated_settings):
    _save_profile()
    before = isolated_settings.read_bytes()

    _discover(profile_id="saved-profile", llm_api_base="https://new.example/api")

    assert http.get.call_args.args[0] == "https://new.example/api/models"
    assert http.get.call_args.kwargs["headers"]["Authorization"] == "Bearer test-explicit-key"
    assert isolated_settings.read_bytes() == before


@pytest.mark.parametrize("key", ["", "   ", "•••mask"])
def test_unsaved_profile_needs_explicit_key(http, key):
    with pytest.raises(HTTPException) as error:
        _discover(llm_api_key=key)
    assert error.value.detail["code"] == "api_key_required"
    http.get.assert_not_called()


def test_unknown_profile_has_structured_error_before_http(http):
    with TestClient(server.app) as client:
        response = client.post(
            "/api/settings/models",
            json={"profile_id": "missing", "llm_api_base": "https://provider.example/v1"},
        )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "profile_not_found"
    http.get.assert_not_called()


@pytest.mark.parametrize(
    "base",
    [
        "",
        "ftp://provider.example/v1",
        "https://user:secret@provider.example/v1",
        "https://provider.example/v1?key=secret",
        "https://provider.example/v1#fragment",
        "https://provider.example/v1?",
        "https://provider.example/v1#",
        "https://provider.example:99999/v1",
        "https://provider.example/with space",
        "https://provider.example\\@other.example/v1",
        "https://provider.example/\nsecret",
    ],
)
def test_invalid_api_base_is_rejected_without_http(http, base):
    with pytest.raises(HTTPException) as error:
        _discover(llm_api_base=base)
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "invalid_api_base"
    assert "secret" not in error.value.detail["message"]
    http.get.assert_not_called()


def test_invalid_route_is_rejected_without_http(http):
    with pytest.raises(HTTPException) as error:
        _discover(route_type="unknown")
    assert error.value.detail["code"] == "invalid_route"
    http.get.assert_not_called()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (301, "upstream_redirect"),
        (307, "upstream_redirect"),
        (401, "upstream_auth"),
        (403, "upstream_auth"),
        (429, "upstream_http"),
        (500, "upstream_http"),
    ],
)
def test_provider_status_errors_never_return_upstream_body(http, status, code):
    http.get.return_value.status_code = status
    http.get.return_value.text = "upstream echoed test-explicit-key"
    http.get.return_value.headers = {"Location": "https://unrelated.example/"}

    with pytest.raises(HTTPException) as error:
        _discover()

    assert error.value.status_code == 502
    assert error.value.detail["code"] == code
    assert "test-explicit-key" not in json.dumps(error.value.detail)
    assert http.get.call_args.kwargs["allow_redirects"] is False
    http.get.assert_called_once()
    http.get.return_value.json.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (requests.Timeout("test-explicit-key"), 504, "upstream_timeout"),
        (requests.ConnectionError("test-explicit-key"), 502, "upstream_connection"),
    ],
)
def test_transport_errors_are_sanitized(http, failure, status, code):
    http.get.side_effect = failure
    with pytest.raises(HTTPException) as error:
        _discover()
    assert error.value.status_code == status
    assert error.value.detail["code"] == code
    assert "test-explicit-key" not in json.dumps(error.value.detail)


@pytest.mark.parametrize("payload", [None, [], {}, {"data": {}}, {"data": [{}]}, {"data": [{"id": 7}]}])
def test_malformed_catalog_returns_structured_error(http, payload):
    http.get.return_value.json.return_value = payload
    with pytest.raises(HTTPException) as error:
        _discover()
    assert error.value.detail["code"] == "invalid_response"


def test_non_json_response_is_sanitized(http):
    http.get.return_value.json.side_effect = ValueError("test-explicit-key")
    with pytest.raises(HTTPException) as error:
        _discover()
    assert error.value.detail["code"] == "invalid_response"
    assert "test-explicit-key" not in json.dumps(error.value.detail)


def test_empty_catalog_returns_distinct_error(http):
    http.get.return_value.json.return_value = {"data": []}
    with pytest.raises(HTTPException) as error:
        _discover()
    assert error.value.detail["code"] == "empty_models"
