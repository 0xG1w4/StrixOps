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
import contextlib
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.sandbox.manifest import Environment, Manifest

from strixops.runtime import caido

DEFAULT_IMAGE = "strixops-sandbox:1.3.0"

_IMAGE_ENV = "STRIXOPS_IMAGE"
_OWNER_LABEL = "io.strixops.run-owner"

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
    _container_id: str = ""
    _owner_token: str = ""
    _daemon_id: str = ""
    quiescence: dict[str, Any] = field(default_factory=dict, init=False)
    cleanup: dict[str, Any] = field(default_factory=dict, init=False)
    _quiesce_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _cleanup_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    def _tracked(self) -> bool:
        return bool(
            self._container_id or self._owner_token or getattr(self.session, "state", None) is not None
        )

    def _owned_container(self) -> Any:
        import docker

        if not self._container_id or not self._owner_token:
            raise RuntimeError("Sandbox container ownership was not captured")
        state_id = getattr(getattr(self.session, "state", None), "container_id", None)
        if state_id != self._container_id:
            raise RuntimeError("Sandbox session container identity changed")
        inner = getattr(self.session, "_inner", None)
        cached_container = getattr(inner, "_container", None)
        if cached_container is not None and cached_container.id != self._container_id:
            raise RuntimeError("Sandbox cached container identity changed")
        try:
            container = self.client.docker_client.containers.get(self._container_id)
        except docker.errors.NotFound:
            return None
        if container.id != self._container_id:
            raise RuntimeError("Sandbox container identity mismatch")
        container.reload()
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if labels.get(_OWNER_LABEL) != self._owner_token:
            raise RuntimeError("Sandbox container ownership label mismatch")
        return container

    def _stop_owned_container(self) -> str:
        container = self._owned_container()
        if container is None:
            return "absent"
        running = container.attrs.get("State", {}).get("Running")
        if running is not False:
            container.stop(timeout=10)
        # Never use SDK running(): it intentionally falls back to cached state
        # when Docker inspection fails.
        container = self._owned_container()
        if container is None:
            return "absent"
        if container.attrs.get("State", {}).get("Running") is not False:
            raise RuntimeError("Sandbox container is still running after stop")
        return "stopped"

    async def _quiesce(self, diagnostics_dir: Path | str | None) -> None:
        self.quiescence = {"status": "pending", "container_id": self._container_id, "verified": False}
        if diagnostics_dir is not None and self.caido is not None:
            with _suppress_all():
                await caido.collect_diagnostics(self.session, diagnostics_dir)
        if self.caido is not None:
            try:
                if hasattr(self.caido, "aclose"):
                    await self.caido.aclose()
                else:
                    await asyncio.to_thread(self.caido.close)
            except (asyncio.CancelledError, Exception) as exc:
                self.quiescence["caido_close_error_type"] = type(exc).__name__
        try:
            if self._tracked():
                status = await asyncio.to_thread(self._stop_owned_container)
                self.quiescence.update(status=status, verified=True)
            else:
                # Compatibility for in-process test doubles without a Docker
                # state. This is explicitly not a verified container cleanup.
                self.quiescence["status"] = "unverified"
        except BaseException as exc:
            self.quiescence.update(status="failed", error_type=type(exc).__name__)
            raise

    async def quiesce(self, *, diagnostics_dir: Path | str | None = None) -> None:
        """Stop the owned container before copying its host-mounted evidence."""
        if self._quiesce_task is None:
            self._quiesce_task = asyncio.create_task(self._quiesce(diagnostics_dir))
        await _join_cleanup(self._quiesce_task)

    async def _teardown(self, diagnostics_dir: Path | str | None) -> None:
        if self.session is None and self.cleanup.get("verified") and self.cleanup.get("status") == "removed":
            # Initialization conclusively finished before any container existed.
            return
        self.cleanup = {"status": "pending", "container_id": self._container_id, "verified": False}
        try:
            # A failed stop must block evidence publication, but must not
            # prevent the final SDK removal attempt.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.quiesce(diagnostics_dir=diagnostics_dir)
            tracked = self._tracked()
            if tracked:
                await asyncio.to_thread(self._owned_container)
            await self.client.delete(self.session)
            if tracked:
                remaining = await asyncio.to_thread(self._owned_container)
                if remaining is not None:
                    raise RuntimeError("Sandbox container still exists after SDK delete")
                self.cleanup.update(status="removed", verified=True)
            else:
                self.cleanup["status"] = "unverified"
            if close_error := self.quiescence.get("caido_close_error_type"):
                self.cleanup["caido_close_error_type"] = close_error
                raise RuntimeError("Caido client cleanup failed")
        except BaseException as exc:
            self.cleanup.update(status="failed", error_type=type(exc).__name__)
            raise

    async def teardown(self, *, diagnostics_dir: Path | str | None = None) -> None:
        """Delete once through the SDK and independently verify container absence."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._teardown(diagnostics_dir))
        await _join_cleanup(self._cleanup_task)


async def _join_cleanup(task: asyncio.Task[Any]) -> Any:
    """Finish one cleanup task despite repeated cancellation of its caller."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    try:
        result = task.result()
    except BaseException as exc:
        if interrupted:
            raise asyncio.CancelledError from exc
        raise
    if interrupted:
        raise asyncio.CancelledError
    return result


class _MountInjectingContainers:
    """Proxy over ``dockerClient.containers`` that prepends bind mounts.

    The SDK builds container kwargs internally; intercepting
    ``containers.create`` lets us add the host-workspace bind without
    duplicating SDK container-creation logic.
    """

    def __init__(
        self,
        inner: Any,
        extra_mounts: list[Any],
        *,
        preserve_image_entrypoint: bool = False,
        owner_token: str = "",
    ) -> None:
        self._inner = inner
        self._extra_mounts = list(extra_mounts)
        self._preserve_image_entrypoint = preserve_image_entrypoint
        self.owner_token = owner_token
        self.created_container: Any = None

    def create(self, **kwargs: Any) -> Any:
        mounts = list(kwargs.get("mounts") or [])
        kwargs["mounts"] = [*self._extra_mounts, *mounts]
        if self.owner_token:
            kwargs["labels"] = {**(kwargs.get("labels") or {}), _OWNER_LABEL: self.owner_token}
        if self._preserve_image_entrypoint:
            # The SDK overrides ENTRYPOINT with tail. Caido-enabled scans need
            # the image entrypoint to start Caido and initialize certificate trust.
            kwargs.pop("entrypoint", None)
            kwargs["command"] = ["tail", "-f", "/dev/null"]
        container = self._inner.create(**kwargs)
        self.created_container = container
        return container

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ContainersInjectedDockerClient:
    """Docker client facade with an instrumented ``containers`` collection."""

    def __init__(
        self,
        inner: Any,
        extra_mounts: list[Any],
        *,
        preserve_image_entrypoint: bool = False,
        owner_token: str = "",
    ) -> None:
        self._inner = inner
        self.containers = _MountInjectingContainers(
            inner.containers,
            extra_mounts,
            preserve_image_entrypoint=preserve_image_entrypoint,
            owner_token=owner_token,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _host_workspace_client(
    docker_client: Any,
    host_dir: str,
    *,
    preserve_image_entrypoint: bool = False,
    owner_token: str = "",
) -> Any:
    """Wrap a docker client so every created container binds ``host_dir`` at
    ``/workspace`` (first mount — overrides image/volume mounts there)."""
    from pathlib import Path as _Path

    import docker

    source = _Path(host_dir).expanduser().resolve()
    source.mkdir(parents=True, exist_ok=True)
    mount = docker.types.Mount(target="/workspace", source=str(source), type="bind", read_only=False)
    return _ContainersInjectedDockerClient(
        docker_client, [mount], preserve_image_entrypoint=preserve_image_entrypoint, owner_token=owner_token
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
    on_created: Callable[[SandboxBundle], None] | None = None,
) -> SandboxBundle:
    """Bring up the sandbox container and return the (client, session) bundle."""
    import docker
    from agents.sandbox.sandboxes.docker import DockerSandboxClient

    image = image or image_for_scan_type(scan_type)
    _ensure_image(image)

    docker_client = docker.from_env()
    owner_token = uuid.uuid4().hex
    if host_workspace_dir:
        # Platform mode: the whole /workspace lives on the host (phase dir),
        # persisted by the platform after the run.
        docker_client = _host_workspace_client(
            docker_client,
            host_workspace_dir,
            preserve_image_entrypoint=use_caido,
            owner_token=owner_token,
        )
    else:
        docker_client = _ContainersInjectedDockerClient(
            docker_client, [], preserve_image_entrypoint=use_caido, owner_token=owner_token
        )
    client = DockerSandboxClient(docker_client)
    from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions

    exposed_ports = (caido.CAIDO_CONTAINER_PORT,) if use_caido else ()
    env = container_environment(scan_type, socks5_proxy, gsocket_key)
    if use_caido:
        env.update(caido.proxy_environment())
    options = DockerSandboxClientOptions(image=image, exposed_ports=exposed_ports)
    manifest = Manifest(environment=Environment(value=env))
    daemon_id = docker_client.info().get("ID", "") if callable(getattr(docker_client, "info", None)) else ""
    bundle = SandboxBundle(client=client, session=None, _owner_token=owner_token, _daemon_id=daemon_id)
    if on_created is not None:
        # Give the runner a cleanup handle before the SDK can create resources.
        # It remains available when initialization raises before returning.
        on_created(bundle)

    def capture_container() -> None:
        container = docker_client.containers.created_container
        container_id = getattr(container, "id", None)
        if isinstance(container_id, str) and container_id:
            bundle._container_id = container_id
            bundle._owner_token = owner_token
            if on_created is not None:
                on_created(bundle)
            if bundle.session is None:
                # SDK create starts Docker before returning a session. If that
                # start fails, reconstruct only its cleanup handle; this manifest
                # has no SDK-managed volumes or snapshot to recover.
                from agents.sandbox.sandboxes.docker import DockerSandboxSession, DockerSandboxSessionState
                from agents.sandbox.snapshot import resolve_snapshot

                session_id = uuid.uuid4()
                state = DockerSandboxSessionState(
                    image=image,
                    container_id=container_id,
                    session_id=session_id,
                    manifest=manifest,
                    snapshot=resolve_snapshot(None, str(session_id)),
                    exposed_ports=exposed_ports,
                )
                bundle.session = client._wrap_session(
                    DockerSandboxSession(docker_client=docker_client, container=container, state=state)
                )

    async def initialize() -> SandboxBundle:
        try:
            bundle.session = await client.create(options=options, manifest=manifest)
            capture_container()
            if bundle._tracked():
                await asyncio.to_thread(bundle._owned_container)
            await bundle.session.start()
            if use_caido:
                endpoint = await bundle.session.resolve_exposed_port(caido.CAIDO_CONTAINER_PORT)
                scheme = "https" if endpoint.tls else "http"
                host_url = f"{scheme}://{endpoint.host}:{endpoint.port}"
                bundle.caido = caido.CaidoBootstrapHandle(
                    asyncio.create_task(
                        caido.bootstrap_caido(bundle.session, host_url=host_url), name="caido-bootstrap"
                    )
                )
        except BaseException:
            capture_container()
            if bundle.session is not None:
                await bundle.teardown()
            else:
                bundle.cleanup = {"status": "removed", "verified": True, "container_id": ""}
            raise
        return bundle

    initialization = asyncio.create_task(initialize())
    try:
        return await asyncio.shield(initialization)
    except asyncio.CancelledError as cancellation:
        initialization.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await _join_cleanup(initialization)
            # Cancellation can race with a successfully completed initialization.
            if bundle.session is not None:
                await bundle.teardown()
        except BaseException as exc:
            raise cancellation from exc
        raise cancellation


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
