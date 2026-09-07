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


def test_legacy_profile_options_are_explicit_without_changing_storage(store_path):
    profile = _make()
    public = settings_store.public_profile(profile)
    env = settings_store.effective_llm(profile, "web")
    assert public["api_mode_web"] == public["api_mode_internal"] == "chat_completions"
    assert public["reasoning_effort_web"] == public["reasoning_effort_internal"] == "default"
    assert env["llm_api_mode"] == "chat_completions"
    assert env["llm_reasoning_effort"] == "default"
    assert "api_mode_web" not in profile


@pytest.mark.parametrize("empty_slot", ["web", "internal"])
def test_model_fallback_inherits_protocol_and_effort_together(store_path, empty_slot):
    populated = "internal" if empty_slot == "web" else "web"
    profile, errors = settings_store.sanitize_profile(
        _make(
            **{
                f"model_{empty_slot}": "",
                f"model_{populated}": "deployment-alias",
                f"api_mode_{empty_slot}": "chat_completions",
                f"reasoning_effort_{empty_slot}": "none",
                f"api_mode_{populated}": "responses",
                f"reasoning_effort_{populated}": "high",
            }
        )
    )
    assert not errors
    env = settings_store.effective_llm(profile, empty_slot)
    assert env["strix_llm"] == "deployment-alias"
    assert env["llm_api_mode"] == "responses"
    assert env["llm_reasoning_effort"] == "high"


def test_patch_preserves_omitted_options_and_allows_explicit_reset_and_clear(store_path):
    profile, errors = settings_store.sanitize_profile(
        _make(api_mode_web="responses", reasoning_effort_web="high")
    )
    assert not errors
    renamed, errors = settings_store.sanitize_profile({"name": "Renamed"}, existing=profile)
    assert not errors
    assert renamed["api_mode_web"] == "responses"
    assert renamed["reasoning_effort_web"] == "high"
    reset, errors = settings_store.sanitize_profile(
        {"reasoning_effort_web": "default", "model_internal": ""}, existing=renamed
    )
    assert not errors
    assert reset["reasoning_effort_web"] == "default"
    assert reset["api_mode_web"] == "responses"
    assert reset["model_internal"] == ""


@pytest.mark.parametrize(
    "options", [{"api_mode_web": "unknown"}, {"reasoning_effort_internal": "impossible"}]
)
def test_invalid_model_option_is_rejected(store_path, options):
    _, errors = settings_store.sanitize_profile(_make(**options))
    assert errors


def test_masked_key_cannot_be_saved_without_a_source(store_path):
    profile, errors = settings_store.sanitize_profile(_make(llm_api_key="•••cret"))
    assert errors
    assert profile["llm_api_key"] == ""


@pytest.mark.parametrize("copy", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        {"llm_api_base": "https://different.example/v1"},
        {"llm_api_base": "https://provider.example/v2"},
        {"llm_api_base": "http://provider.example/v1"},
        {"llm_api_base": "https://provider.example:444/v1"},
        {"route_type": "openrouter"},
    ],
)
def test_reusing_key_requires_same_provider_and_endpoint(store_path, change, copy):
    source, errors = settings_store.sanitize_profile(
        _make(route_type="custom", llm_api_base="https://provider.example/v1")
    )
    assert not errors
    kwargs = {"copy_source" if copy else "existing": source}
    profile, errors = settings_store.sanitize_profile({"llm_api_key": "•••-key", **change}, **kwargs)
    assert any("enter a new API key" in error for error in errors)
    assert not profile["llm_api_key"]
    replacement, errors = settings_store.sanitize_profile(
        {"llm_api_key": "new-endpoint-key", **change}, **kwargs
    )
    assert not errors
    assert replacement["llm_api_key"] == "new-endpoint-key"
