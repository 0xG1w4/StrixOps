"""Docker sandbox session lifecycle on the openai-agents SDK sandbox stack.

The SDK's ``DockerSandboxClient`` brings up the container; ``Shell`` /
``Filesystem`` capabilities on a ``SandboxAgent`` speak to it. This module
owns image resolution, the container environment, and cleanup.

Web and internal scans use the same complete image, built at deploy time.
An explicitly configured registry image can be pulled when missing. The run
directory and initial events already exist before image preparation begins.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.sandbox.manifest import Environment, Manifest

from strixops.runtime import caido

DEFAULT_IMAGE = "strixops-sandbox:1.3.0"

_IMAGE_ENV = "STRIXOPS_IMAGE"

logger = logging.getLogger("strixops.sandbox")


def image_for_scan_type(scan_type: str) -> str:
    """Both scan modes use the configured complete sandbox image."""
    return default_image()


def default_image() -> str:
    return (os.environ.get(_IMAGE_ENV) or "").strip() or DEFAULT_IMAGE


@dataclass
class SandboxBundle:
    client: Any
    session: Any
    caido: Any = None  # CaidoBootstrapHandle (or ready CaidoClient)

    async def teardown(self, *, diagnostics_dir: Path | str | None = None) -> None:
        if diagnostics_dir is not None and self.caido is not None:
            with _suppress_all():
                await caido.collect_diagnostics(self.session, diagnostics_dir)
        if self.caido is not None:
            with _suppress_all():
                if hasattr(self.caido, "aclose"):
                    await self.caido.aclose()
                else:
                    await asyncio.to_thread(self.caido.close)
        with _suppress_all():
            await self.client.delete(self.session)


class _MountInjectingContainers:
    """Proxy over ``dockerClient.containers`` that prepends bind mounts.

    The SDK builds container kwargs internally; intercepting
    ``containers.create`` lets us add the host-workspace bind without
    duplicating SDK container-creation logic.
    """

    def __init__(
        self, inner: Any, extra_mounts: list[Any], *, preserve_image_entrypoint: bool = False
    ) -> None:
        self._inner = inner
        self._extra_mounts = list(extra_mounts)
        self._preserve_image_entrypoint = preserve_image_entrypoint

    def create(self, **kwargs: Any) -> Any:
        mounts = list(kwargs.get("mounts") or [])
        kwargs["mounts"] = [*self._extra_mounts, *mounts]
        if self._preserve_image_entrypoint:
            # The SDK overrides ENTRYPOINT with tail. Caido-enabled scans need
            # the image entrypoint to start Caido and initialize certificate trust.
            kwargs.pop("entrypoint", None)
            kwargs["command"] = ["tail", "-f", "/dev/null"]
        return self._inner.create(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ContainersInjectedDockerClient:
    """Docker client facade with an instrumented ``containers`` collection."""

    def __init__(
        self, inner: Any, extra_mounts: list[Any], *, preserve_image_entrypoint: bool = False
    ) -> None:
        self._inner = inner
        self.containers = _MountInjectingContainers(
            inner.containers, extra_mounts, preserve_image_entrypoint=preserve_image_entrypoint
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _host_workspace_client(
    docker_client: Any, host_dir: str, *, preserve_image_entrypoint: bool = False
) -> Any:
    """Wrap a docker client so every created container binds ``host_dir`` at
    ``/workspace`` (first mount — overrides image/volume mounts there)."""
    from pathlib import Path as _Path

    import docker

    source = _Path(host_dir).expanduser().resolve()
    source.mkdir(parents=True, exist_ok=True)
    mount = docker.types.Mount(target="/workspace", source=str(source), type="bind", read_only=False)
    return _ContainersInjectedDockerClient(
        docker_client, [mount], preserve_image_entrypoint=preserve_image_entrypoint
    )


def _suppress_all():
    import contextlib

    return contextlib.suppress(Exception)


def container_environment(scan_type: str, socks5_proxy: str = "", gsocket_key: str = "") -> dict[str, str]:
    """Container env. Internal mode carries the tunnel credentials."""
    env = {
        "PYTHONUNBUFFERED": "1",
        "HOST_GATEWAY": "host.docker.internal",
    }
    if scan_type == "internal":
        if socks5_proxy:
            env["SOCKS5_PROXY"] = socks5_proxy
        if gsocket_key:
            env["GSOCKET_KEY"] = gsocket_key
    return env


async def create_sandbox_session(
    *,
    image: str | None = None,
    scan_type: str = "web",
    socks5_proxy: str = "",
    gsocket_key: str = "",
    host_workspace_dir: str = "",
    use_caido: bool = False,
) -> SandboxBundle:
    """Bring up the sandbox container and return the (client, session) bundle."""
    import docker
    from agents.sandbox.sandboxes.docker import DockerSandboxClient

    image = image or image_for_scan_type(scan_type)
    _ensure_image(image)

    docker_client = docker.from_env()
    if host_workspace_dir:
        # Platform mode: the whole /workspace lives on the host (phase dir),
        # persisted by the platform after the run.
        docker_client = _host_workspace_client(
            docker_client, host_workspace_dir, preserve_image_entrypoint=use_caido
        )
    elif use_caido:
        docker_client = _ContainersInjectedDockerClient(docker_client, [], preserve_image_entrypoint=True)
    client = DockerSandboxClient(docker_client)
    from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions

    exposed_ports = (caido.CAIDO_CONTAINER_PORT,) if use_caido else ()
    env = container_environment(scan_type, socks5_proxy, gsocket_key)
    if use_caido:
        env.update(caido.proxy_environment())
    options = DockerSandboxClientOptions(image=image, exposed_ports=exposed_ports)
    manifest = Manifest(environment=Environment(value=env))
    session = await client.create(options=options, manifest=manifest)
    bundle = SandboxBundle(client=client, session=session)
    try:
        await session.start()
        if use_caido:
            endpoint = await session.resolve_exposed_port(caido.CAIDO_CONTAINER_PORT)
            scheme = "https" if endpoint.tls else "http"
            host_url = f"{scheme}://{endpoint.host}:{endpoint.port}"
            bundle.caido = caido.CaidoBootstrapHandle(
                asyncio.create_task(caido.bootstrap_caido(session, host_url=host_url), name="caido-bootstrap")
            )
    except BaseException:
        await bundle.teardown()
        raise

    return bundle


def _ensure_image(image: str) -> None:
    """Require the local build, or pull an explicitly configured registry image.

    Confirm ImageNotFound after waking an idle daemon. Docker daemon/socket
    errors must retain their actual cause instead of being reported as build errors.
    """
    import docker

    docker_client = None
    try:
        docker_client = docker.from_env()
        try:
            docker_client.images.get(image)
            return
        except docker.errors.ImageNotFound:
            # Docker Desktop Resource Saver can serve a stale cached 404 while
            # its VM sleeps. Listing images wakes the daemon; inspect again
            # before concluding that the requested image needs to be built.
            docker_client.images.list()
        try:
            docker_client.images.get(image)
            return
        except docker.errors.ImageNotFound:
            if image.split(":", 1)[0] == "strixops-sandbox":
                import shlex

                tag = image.partition(":")[2] or "1.3.0"
                raise RuntimeError(
                    f"Sandbox image {image!r} is not built. "
                    f"Run: bash containers/build-images.sh {shlex.quote(tag)}"
                ) from None
        docker_client.images.pull(image)
    except docker.errors.DockerException as exc:
        raise RuntimeError(f"Docker could not prepare sandbox image {image!r}: {exc}") from exc
    finally:
        if docker_client is not None:
            with _suppress_all():
                docker_client.close()
