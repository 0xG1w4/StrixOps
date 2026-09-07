"""Model-profile store tests: CRUD semantics, masking, env resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from strixops.console import settings_store


@pytest.fixture()
def store_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "console.json"
    monkeypatch.setenv("STRIXOPS_CONSOLE_CONFIG", str(path))
    return path


def _make(**overrides):
    payload = {
        "name": "OpenRouter main",
        "route_type": "openrouter",
        "llm_api_key": "test-key",
        "model_web": "openrouter/z-ai/glm-5.3",
        "model_internal": "openrouter/openai/gpt-5",
    }
    payload.update(overrides)
    return payload


def test_create_openrouter_pins_base_and_strips_prefix(store_path):
    profile, errors = settings_store.sanitize_profile(_make())
    assert errors == []
    assert profile["llm_api_base"] == settings_store.OPENROUTER_API_BASE
    assert profile["model_web"] == "z-ai/glm-5.3"
    assert profile["model_internal"] == "openai/gpt-5"


def test_custom_requires_base(store_path):
    profile, errors = settings_store.sanitize_profile(_make(route_type="custom", llm_api_base=""))
    assert any("API base" in e for e in errors)


def test_masked_key_roundtrip_keeps_stored_key(store_path):
    created, _ = settings_store.sanitize_profile(_make())
    masked = settings_store.mask_key(created["llm_api_key"])
    assert settings_store.is_masked(masked)
    updated, errors = settings_store.sanitize_profile(
        _make(name="renamed", llm_api_key=masked), existing=created
    )
    assert errors == []
    assert updated["llm_api_key"] == created["llm_api_key"]


def test_at_least_one_model(store_path):
    _, errors = settings_store.sanitize_profile(_make(model_web="", model_internal=""))
    assert any("model" in e for e in errors)


def test_effective_llm_per_scan_type(store_path):
    profile, _ = settings_store.sanitize_profile(_make())
    web = settings_store.effective_llm(profile, "web")
    internal = settings_store.effective_llm(profile, "internal")
    assert web["strix_llm"] == "z-ai/glm-5.3"
    assert internal["strix_llm"] == "openai/gpt-5"
    assert web["llm_api_base"] == settings_store.OPENROUTER_API_BASE


def test_effective_llm_fallback_when_one_model_missing(store_path):
    profile, _ = settings_store.sanitize_profile(_make(model_internal=""))
    assert settings_store.effective_llm(profile, "internal")["strix_llm"] == "z-ai/glm-5.3"


def test_settings_persistence_roundtrip(store_path):
    data = settings_store.load_settings()
    profile, _ = settings_store.sanitize_profile(_make())
    data["profiles"].append(profile)
    data["active_profile_id"] = profile["id"]
    settings_store.save_settings(data)

    loaded = settings_store.load_settings()
    assert loaded["active_profile_id"] == profile["id"]
    assert loaded["profiles"][0]["llm_api_key"] == profile["llm_api_key"]
    # file is 0600 — key material never world-readable
    assert (store_path.stat().st_mode & 0o777) == 0o600


def test_public_profile_masks_key(store_path):
    profile, _ = settings_store.sanitize_profile(_make())
    public = settings_store.public_profile(profile)
    assert public["llm_api_key"].startswith("•••")
    assert profile["llm_api_key"] not in public["llm_api_key"]
    assert public["llm_api_key_set"] is True
