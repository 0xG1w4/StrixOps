"""Model profile API contracts and launch configuration isolation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from strixops.console import server, settings_store


@pytest.fixture()
def profile_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("STRIXOPS_CONSOLE_CONFIG", str(tmp_path / "console.json"))
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(server, "state", server.ConsoleState(runs_root))
    with TestClient(server.app) as client:
        yield client


def _source_profile() -> dict:
    source, errors = settings_store.sanitize_profile(
        {
            "name": "Existing route",
            "route_type": "custom",
            "llm_api_base": "https://provider.example/v1/",
            "llm_api_key": "saved-test-secret",
            "model_web": "gpt-6-astra",
            "model_internal": "",
            "api_mode_web": "auto",
            "reasoning_effort_web": "high",
        }
    )
    assert not errors
    source["created_at"] = "2025-01-01T00:00:00Z"
    source["updated_at"] = "2025-01-02T00:00:00Z"
    settings_store.save_settings({"profiles": [source], "active_profile_id": source["id"]})
    return source


def test_copy_inherits_options_and_server_key_with_new_identity(profile_api: TestClient) -> None:
    source = _source_profile()
    response = profile_api.post(
        "/api/settings/profiles",
        json={
            "copy_from_profile_id": source["id"],
            "name": "Copied route",
            "llm_api_base": "HTTPS://PROVIDER.EXAMPLE:443/v1",
            "llm_api_key": settings_store.mask_key(source["llm_api_key"]),
            "reasoning_effort_web": "low",
        },
    )

    assert response.status_code == 200, response.text
    public = response.json()["profile"]
    assert public["id"] != source["id"]
    assert public["created_at"] != source["created_at"]
    assert public["updated_at"] != source["updated_at"]
    assert public["api_mode_web"] == "auto"
    assert public["reasoning_effort_web"] == "low"
    assert "copy_from_profile_id" not in public
    assert source["llm_api_key"] not in response.text
    data = settings_store.load_settings()
    assert data["profiles"][0] == source
    assert data["profiles"][1]["llm_api_key"] == source["llm_api_key"]
    assert data["active_profile_id"] == source["id"]
    assert source["llm_api_key"] not in profile_api.get("/api/settings").text


@pytest.mark.parametrize("key", ["", "•••cret"])
def test_copy_changed_endpoint_requires_actual_new_key(profile_api: TestClient, key: str) -> None:
    source = _source_profile()
    before = settings_store.config_path().read_bytes()
    response = profile_api.post(
        "/api/settings/profiles",
        json={
            "copy_from_profile_id": source["id"],
            "name": "Copied route",
            "llm_api_base": "https://different.example/v1",
            "llm_api_key": key,
        },
    )
    assert response.status_code == 400
    assert "new API key" in response.text
    assert source["llm_api_key"] not in response.text
    assert settings_store.config_path().read_bytes() == before


def test_missing_copy_source_does_not_create_or_modify_profile(profile_api: TestClient) -> None:
    _source_profile()
    before = settings_store.config_path().read_bytes()
    response = profile_api.post(
        "/api/settings/profiles", json={"copy_from_profile_id": "deleted-profile", "name": "Copy"}
    )
    assert response.status_code == 404
    assert settings_store.config_path().read_bytes() == before


def test_patch_keeps_omitted_fields_and_explicit_default_resets_effort(profile_api: TestClient) -> None:
    source = _source_profile()
    response = profile_api.patch(f"/api/settings/profiles/{source['id']}", json={"name": "Renamed"})
    assert response.status_code == 200
    profile = response.json()["profile"]
    assert profile["route_type"] == "custom"
    assert profile["llm_api_base"] == source["llm_api_base"]
    assert profile["api_mode_web"] == "auto"
    assert profile["reasoning_effort_web"] == "high"
    response = profile_api.patch(
        f"/api/settings/profiles/{source['id']}", json={"reasoning_effort_web": "default"}
    )
    assert response.status_code == 200
    assert response.json()["profile"]["reasoning_effort_web"] == "default"


@pytest.mark.parametrize("scan_type", ["web", "internal"])
def test_launch_uses_profile_options_and_snapshots_resolved_api(
    profile_api: TestClient, monkeypatch: pytest.MonkeyPatch, scan_type: str
) -> None:
    source = _source_profile()
    monkeypatch.setenv("LLM_API_MODE", "chat_completions")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "none")
    process = Mock(pid=12345)
    launch = Mock(return_value=process)
    monkeypatch.setattr(server.subprocess, "Popen", launch)

    response = profile_api.post(
        "/api/scans",
        json={"target": "https://example.com" if scan_type == "web" else "10.0.0.1", "scan_type": scan_type},
    )

    assert response.status_code == 200, response.text
    env = launch.call_args.kwargs["env"]
    assert env["STRIX_LLM"] == "gpt-6-astra"
    assert env["LLM_API_MODE"] == "auto"
    assert env["LLM_REASONING_EFFORT"] == "high"
    assert env["LLM_API_KEY"] == source["llm_api_key"]
    run_dir = server.state.runs_root / response.json()["run_name"]
    metadata_text = (run_dir / server.LAUNCH_SIDECAR).read_text(encoding="utf-8")
    metadata = json.loads(metadata_text)
    assert metadata["llm_api_mode"] == "responses"
    assert metadata["llm_api_mode_requested"] == "auto"
    assert metadata["llm_reasoning_effort"] == "high"
    assert source["llm_api_key"] not in metadata_text
    assert source["llm_api_key"] not in response.text


def test_inline_default_effort_overrides_ambient_setting(
    profile_api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_API_MODE", "responses")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    launch = Mock(return_value=Mock(pid=12345))
    monkeypatch.setattr(server.subprocess, "Popen", launch)
    response = profile_api.post(
        "/api/scans",
        json={
            "target": "https://example.com",
            "llm_api_base": "https://provider.example/v1",
            "llm_api_key": "inline-secret",
            "strix_llm": "custom-model",
        },
    )
    assert response.status_code == 200, response.text
    env = launch.call_args.kwargs["env"]
    assert env["LLM_API_MODE"] == "chat_completions"
    assert env["LLM_REASONING_EFFORT"] == "default"


@pytest.mark.parametrize(("model", "effort"), [("gpt-6-astra", "high"), ("gpt-5.5", "default")])
def test_create_and_launch_preserve_explicit_gpt_chat_route(
    profile_api: TestClient, monkeypatch: pytest.MonkeyPatch, model: str, effort: str,
) -> None:
    created = profile_api.post(
        "/api/settings/profiles",
        json={
            "name": "Gateway Chat route", "route_type": "custom",
            "llm_api_base": "https://provider.example/v1", "llm_api_key": "saved-test-secret",
            "model_web": model, "api_mode_web": "chat_completions", "reasoning_effort_web": effort,
        },
    )
    assert created.status_code == 200, created.text
    profile = created.json()["profile"]
    assert profile["api_mode_web"] == "chat_completions"
    assert profile["reasoning_effort_web"] == effort
    monkeypatch.setenv("LLM_API_MODE", "responses")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    launch = Mock(return_value=Mock(pid=12345))
    monkeypatch.setattr(server.subprocess, "Popen", launch)

    response = profile_api.post(
        "/api/scans", json={"target": "https://example.com", "profile_id": profile["id"]},
    )
    assert response.status_code == 200, response.text
    env = launch.call_args.kwargs["env"]
    assert env["STRIX_LLM"] == model
    assert env["LLM_API_MODE"] == "chat_completions"
    assert env["LLM_REASONING_EFFORT"] == effort
    run_dir = server.state.runs_root / response.json()["run_name"]
    metadata = json.loads((run_dir / server.LAUNCH_SIDECAR).read_text(encoding="utf-8"))
    assert metadata["llm_api_mode"] == "chat_completions"
    assert metadata["llm_api_mode_requested"] == "chat_completions"
    assert metadata["llm_reasoning_effort"] == effort


def test_invalid_api_mode_fails_before_spawning_or_writing_run(
    profile_api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = Mock()
    monkeypatch.setattr(server.subprocess, "Popen", launch)
    response = profile_api.post(
        "/api/scans",
        json={
            "target": "https://example.com",
            "llm_api_base": "https://provider.example/v1",
            "llm_api_key": "inline-secret",
            "strix_llm": "gpt-6-astra",
            "llm_api_mode": "invalid",
            "llm_reasoning_effort": "high",
        },
    )
    assert response.status_code == 400
    assert "inline-secret" not in response.text
    assert list(server.state.runs_root.iterdir()) == []
    launch.assert_not_called()
