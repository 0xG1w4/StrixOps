"""One image for both scan modes, with actionable deployment failures."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, call

import docker
import pytest
from agents.sandbox.sandboxes import docker as sdk_docker

from strixops.runtime import sandbox


@pytest.fixture(autouse=True)
def image_environment(monkeypatch):
    monkeypatch.delenv("STRIXOPS_IMAGE", raising=False)
    monkeypatch.delenv("STRIXOPS_INTERNAL_IMAGE", raising=False)


def test_both_scan_modes_use_the_unified_default():
    assert sandbox.default_image() == "strixops-sandbox:1.3.0"
    assert sandbox.image_for_scan_type("web") == sandbox.default_image()
    assert sandbox.image_for_scan_type("internal") == sandbox.default_image()


def test_global_image_override_applies_to_both_modes(monkeypatch):
    monkeypatch.setenv("STRIXOPS_IMAGE", "  registry.example/sandbox:custom  ")
    assert sandbox.default_image() == "registry.example/sandbox:custom"
    assert sandbox.image_for_scan_type("web") == sandbox.default_image()
    assert sandbox.image_for_scan_type("internal") == sandbox.default_image()


@pytest.mark.parametrize("global_image", ["", "  ", "registry.example/sandbox:custom"])
def test_legacy_internal_override_cannot_split_image_selection(monkeypatch, global_image):
    monkeypatch.setenv("STRIXOPS_IMAGE", global_image)
    monkeypatch.setenv("STRIXOPS_INTERNAL_IMAGE", "legacy-internal:old")
    assert sandbox.image_for_scan_type("internal") == sandbox.image_for_scan_type("web")
    assert sandbox.image_for_scan_type("internal") != "legacy-internal:old"


def test_existing_image_never_pulls_and_closes_lookup_client(monkeypatch):
    client = Mock()
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))

    sandbox._ensure_image(sandbox.default_image())

    client.images.get.assert_called_once_with(sandbox.default_image())
    client.images.list.assert_not_called()
    client.images.pull.assert_not_called()
    client.close.assert_called_once()


@pytest.mark.parametrize("scan_type", ["web", "internal"])
def test_cached_missing_image_is_found_after_waking_daemon(monkeypatch, scan_type):
    client = Mock()
    image = sandbox.image_for_scan_type(scan_type)
    client.images.get.side_effect = [docker.errors.ImageNotFound("cached missing"), Mock()]
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))

    sandbox._ensure_image(image)

    assert client.images.mock_calls == [call.get(image), call.list(), call.get(image)]
    client.close.assert_called_once()


@pytest.mark.parametrize("scan_type", ["web", "internal"])
async def test_missing_default_stops_before_sdk_creation_with_build_command(monkeypatch, scan_type):
    client = Mock()
    client.images.get.side_effect = docker.errors.ImageNotFound("missing")
    from_env = Mock(return_value=client)
    create = AsyncMock()
    monkeypatch.setattr(docker, "from_env", from_env)
    monkeypatch.setattr(sdk_docker.DockerSandboxClient, "create", create)

    with pytest.raises(RuntimeError) as caught:
        await sandbox.create_sandbox_session(scan_type=scan_type)

    assert sandbox.default_image() in str(caught.value)
    assert "bash containers/build-images.sh" in str(caught.value)
    image = sandbox.default_image()
    assert client.images.mock_calls == [call.get(image), call.list(), call.get(image)]
    client.close.assert_called_once()
    from_env.assert_called_once()
    create.assert_not_called()


@pytest.mark.parametrize("scan_type", ["web", "internal"])
async def test_daemon_connection_failure_is_not_reported_as_unbuilt_image(monkeypatch, scan_type):
    failure = docker.errors.DockerException("Cannot connect to Docker daemon")
    create = AsyncMock()
    monkeypatch.setattr(docker, "from_env", Mock(side_effect=failure))
    monkeypatch.setattr(sdk_docker.DockerSandboxClient, "create", create)

    with pytest.raises((docker.errors.DockerException, RuntimeError)) as caught:
        await sandbox.create_sandbox_session(scan_type=scan_type)

    assert "Docker" in str(caught.value)
    assert "not built" not in str(caught.value)
    assert "build-images.sh" not in str(caught.value)
    assert caught.value is failure or caught.value.__cause__ is failure
    create.assert_not_called()


def test_image_inspection_failure_is_not_reported_as_missing(monkeypatch):
    client = Mock()
    failure = docker.errors.APIError("Docker daemon inspection failed")
    client.images.get.side_effect = failure
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))

    with pytest.raises((docker.errors.DockerException, RuntimeError)) as caught:
        sandbox._ensure_image(sandbox.default_image())

    assert "not built" not in str(caught.value)
    assert "build-images.sh" not in str(caught.value)
    assert caught.value is failure or caught.value.__cause__ is failure
    client.images.get.assert_called_once_with(sandbox.default_image())
    client.images.list.assert_not_called()
    client.images.pull.assert_not_called()
    client.close.assert_called_once()


def test_daemon_wake_failure_is_not_reported_as_missing_image(monkeypatch):
    client = Mock()
    client.images.get.side_effect = docker.errors.ImageNotFound("cached missing")
    failure = docker.errors.APIError("Docker daemon wake failed")
    client.images.list.side_effect = failure
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))

    with pytest.raises(RuntimeError) as caught:
        sandbox._ensure_image(sandbox.default_image())

    assert caught.value.__cause__ is failure
    assert "Docker daemon wake failed" in str(caught.value)
    assert "not built" not in str(caught.value)
    assert "build-images.sh" not in str(caught.value)
    assert client.images.mock_calls == [call.get(sandbox.default_image()), call.list()]
    client.close.assert_called_once()


def test_inspection_failure_after_wake_is_not_reported_as_missing_image(monkeypatch):
    client = Mock()
    failure = docker.errors.APIError("Docker daemon inspection failed after wake")
    client.images.get.side_effect = [docker.errors.ImageNotFound("cached missing"), failure]
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))

    with pytest.raises(RuntimeError) as caught:
        sandbox._ensure_image(sandbox.default_image())

    assert caught.value.__cause__ is failure
    assert "not built" not in str(caught.value)
    assert "build-images.sh" not in str(caught.value)
    image = sandbox.default_image()
    assert client.images.mock_calls == [call.get(image), call.list(), call.get(image)]
    client.close.assert_called_once()


def test_custom_missing_image_pulls_and_closes_lookup_client(monkeypatch):
    client = Mock()
    client.images.get.side_effect = docker.errors.ImageNotFound("missing")
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))

    sandbox._ensure_image("registry.example/sandbox:custom")

    image = "registry.example/sandbox:custom"
    assert client.images.mock_calls == [
        call.get(image),
        call.list(),
        call.get(image),
        call.pull(image),
    ]
    client.close.assert_called_once()


def test_custom_pull_failure_surfaces_and_closes_lookup_client(monkeypatch):
    client = Mock()
    client.images.get.side_effect = docker.errors.ImageNotFound("missing")
    failure = docker.errors.APIError("registry unavailable")
    client.images.pull.side_effect = failure
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))

    with pytest.raises((docker.errors.DockerException, RuntimeError)) as caught:
        sandbox._ensure_image("registry.example/sandbox:custom")

    assert caught.value is failure or caught.value.__cause__ is failure
    assert "build-images.sh" not in str(caught.value)
    client.close.assert_called_once()
