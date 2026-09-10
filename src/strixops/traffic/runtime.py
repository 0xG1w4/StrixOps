"""Independent Docker lifecycle for MCP traffic capture and structured replay.

These containers never share the Web/Internal scanner runtime. All Docker
mutations verify task, session, role, and an unguessable persisted owner label.
Proxy listeners default to host loopback. Remote listeners require explicit
binding and credentials; no management API is exposed.
"""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import docker

from .certificates import ca_info
from .http_worker import prepare_request
from .scope import normalize_rules

# Official multi-architecture manifest, verified against the upstream GHCR
# package. Changing version requires updating both the tag and digest.
DEFAULT_IMAGE = (
    "ghcr.io/mitmproxy/mitmproxy:12.2.3@"
    "sha256:00b77b5d8804c8ad18cb6caefbf9d5849e895e8986c5ce011f4ae30f4385962f"
)
OWNER_LABEL = "io.strixops.mcp.owner"
TASK_LABEL = "io.strixops.mcp.task"
SESSION_LABEL = "io.strixops.mcp.session"
ROLE_LABEL = "io.strixops.mcp.role"
START_TIMEOUT = 20
REPLAY_TIMEOUT = 40


class RuntimeUnavailable(RuntimeError):
    """Docker or the explicitly installed capture image is unavailable."""


def _cancelled(cancel: Any) -> bool:
    return bool(cancel() if callable(cancel) else cancel.is_set()) if cancel is not None else False


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=True))
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _policy(task: dict) -> dict:
    source = task.get("scope") or task
    allow = normalize_rules(source.get("allow_hosts", []))
    if not allow:
        raise ValueError("At least one allowed website is required")
    return {
        "allow_hosts": allow,
        "exclude_hosts": normalize_rules(source.get("exclude_hosts", [])),
        "scope_revision": int(source.get("scope_revision", 1)),
    }


def _proxy_settings() -> tuple[dict, str]:
    """Validate listener settings without including credentials in metadata/errors."""
    bind_host = os.environ.get("STRIXOPS_MCP_PROXY_BIND_HOST", "127.0.0.1")
    try:
        if "%" in bind_host:
            raise ValueError
        address = ipaddress.ip_address(bind_host)
        if address.is_multicast:
            raise ValueError
    except ValueError:
        raise ValueError("STRIXOPS_MCP_PROXY_BIND_HOST must be a literal IPv4 or IPv6 address") from None
    bind_host = str(address)
    public_host = os.environ.get("STRIXOPS_MCP_PROXY_PUBLIC_HOST", "")
    if not public_host:
        if address.is_unspecified:
            raise ValueError("Wildcard proxy binding requires STRIXOPS_MCP_PROXY_PUBLIC_HOST")
        public_host = bind_host
    try:
        public_address = ipaddress.ip_address(public_host)
    except ValueError:
        labels = public_host.rstrip(".").split(".")
        if (
            len(public_host) > 253
            or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels
            )
            or re.fullmatch(r"[0-9.]+", public_host)
        ):
            raise ValueError(
                "STRIXOPS_MCP_PROXY_PUBLIC_HOST must be a concrete IP address or DNS hostname"
            ) from None
        public_host = public_host.rstrip(".").lower()
    else:
        if public_address.is_unspecified or public_address.is_multicast or "%" in public_host:
            raise ValueError("STRIXOPS_MCP_PROXY_PUBLIC_HOST must be a concrete IP address or DNS hostname")
        public_host = str(public_address)
    auth = os.environ.get("STRIXOPS_MCP_PROXY_AUTH", "")
    if auth:
        # Mitmproxy accepts exactly one colon and reserves @/ldap prefixes for
        # other authentication backends. Restrict to its literal single user mode.
        parts = auth.split(":")
        if (
            len(parts) != 2
            or not all(re.fullmatch(r"[\x21-\x39\x3b-\x7e]{1,256}", part) for part in parts)
            or parts[0].startswith(("@", "ldap"))
        ):
            raise ValueError(
                "STRIXOPS_MCP_PROXY_AUTH requires username:password with 1–256 printable ASCII "
                "characters per part, no whitespace or extra colon, and no @/ldap username prefix"
            )
    if not address.is_loopback and not auth:
        raise ValueError("Remote proxy binding requires STRIXOPS_MCP_PROXY_AUTH=username:password")
    return {
        "proxy_bind_host": bind_host,
        "proxy_host": public_host,
        "proxy_auth_required": bool(auth),
    }, auth


def proxy_configuration() -> dict:
    """Public connection guidance; never return the proxy password."""
    return _proxy_settings()[0]


def _binding_matches(bindings: list[dict], expected: str) -> bool:
    try:
        address = ipaddress.ip_address(expected)
        return bool(bindings) and all(ipaddress.ip_address(item["HostIp"]) == address for item in bindings)
    except (ValueError, KeyError):
        return False


def _probe_host(bind_host: str) -> str:
    address = ipaddress.ip_address(bind_host)
    return ("127.0.0.1" if address.version == 4 else "::1") if address.is_unspecified else bind_host


class CaptureRuntime:
    def __init__(self, docker_client: Any = None) -> None:
        self._docker = docker_client

    @property
    def client(self):
        if self._docker is None:
            try:
                self._docker = docker.from_env(timeout=5)
            except docker.errors.DockerException as exc:
                raise RuntimeUnavailable(
                    "Docker is unavailable; start Docker and allow daemon access"
                ) from exc
        return self._docker

    def _image(self) -> str:
        try:
            self.client.images.get(DEFAULT_IMAGE)
        except docker.errors.ImageNotFound as exc:
            raise RuntimeUnavailable(
                f"Capture image is not installed. Run: docker pull {DEFAULT_IMAGE}"
            ) from exc
        except docker.errors.DockerException as exc:
            raise RuntimeUnavailable("Cannot inspect Docker images; check daemon access") from exc
        return DEFAULT_IMAGE

    @staticmethod
    def ca_path(directory: Path, *, ca_directory: Path | None = None) -> Path:
        # Only this public certificate may be returned by an API; the adjacent
        # mitmproxy-ca.pem contains the private key and must never be served.
        return (
            Path(ca_directory) if ca_directory is not None else Path(directory) / "ca"
        ) / "mitmproxy-ca-cert.pem"

    @staticmethod
    def _metadata(task_id: str, session_id: str, role: str) -> dict:
        return {
            "task_id": str(task_id),
            "session_id": str(session_id),
            "role": role,
            "owner_token": uuid.uuid4().hex,
            "image": DEFAULT_IMAGE,
        }

    @staticmethod
    def _labels(metadata: dict) -> dict:
        return {
            OWNER_LABEL: metadata["owner_token"],
            TASK_LABEL: metadata["task_id"],
            SESSION_LABEL: metadata["session_id"],
            ROLE_LABEL: metadata["role"],
        }

    @staticmethod
    def _options() -> dict:
        return {
            "detach": True,
            "user": f"{os.getuid()}:{os.getgid()}",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": "384m",
            "pids_limit": 96,
            "nano_cpus": 1_000_000_000,
            "tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
            "environment": {"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
            "extra_hosts": {"host.docker.internal": "host-gateway"},
            "log_config": docker.types.LogConfig(
                type="json-file", config={"max-size": "1m", "max-file": "1"}
            ),
        }

    def _owned(self, task_id: str, session: dict, *, role: str = "capture"):
        if role == "capture" and not session.get("container_id") and session.get("directory"):
            # Docker create and the console DB cannot be one transaction. A
            # private per-session receipt closes the restart-during-start gap.
            receipt = Path(session["directory"]) / "runtime.json"
            if receipt.is_file() and receipt.stat().st_size < 65536:
                saved = json.loads(receipt.read_text())
                identity = str(session.get("session_id") or session.get("id", ""))
                if saved.get("task_id") != str(task_id) or saved.get("session_id") != identity:
                    raise ValueError("MCP runtime receipt does not match this capture session")
                for key in (
                    "container_id",
                    "owner_token",
                    "task_id",
                    "session_id",
                    "proxy_host",
                    "proxy_bind_host",
                    "proxy_auth_required",
                    "proxy_port",
                    "image",
                    "role",
                    "ca_directory",
                    "ca_shared",
                    "ca_sha256",
                ):
                    if key in saved:
                        session[key] = saved[key]
        container_id = session.get("container_id", "")
        if not re.fullmatch(r"[a-f0-9]{64}", str(container_id)) or not session.get("owner_token"):
            raise ValueError("Missing or invalid MCP container ownership record")
        if str(task_id) != str(session.get("task_id", task_id)):
            raise ValueError("MCP container belongs to a different task")
        expected = {
            OWNER_LABEL: session["owner_token"],
            TASK_LABEL: str(task_id),
            SESSION_LABEL: str(session.get("session_id") or session.get("id", "")),
            ROLE_LABEL: role,
        }
        try:
            container = self.client.containers.get(container_id)
            container.reload()
        except docker.errors.NotFound:
            return None
        except docker.errors.DockerException as exc:
            raise RuntimeUnavailable("Cannot inspect the MCP container") from exc
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if container.id != container_id or any(labels.get(key) != value for key, value in expected.items()):
            raise ValueError("MCP container ownership mismatch; refusing to operate on it")
        return container

    def _remove(self, task_id: str, session: dict, *, role: str = "capture") -> None:
        container = self._owned(task_id, session, role=role)
        if container is None:
            return
        if container.attrs.get("State", {}).get("Running"):
            try:
                container.stop(timeout=5)
            except docker.errors.NotFound:
                return  # A replay may auto-remove between inspection and stop.
        # Refresh identity before removal rather than trusting a stale handle.
        container = self._owned(task_id, session, role=role)
        if container is not None:
            if container.attrs.get("State", {}).get("Running"):
                raise RuntimeUnavailable("MCP container did not stop; removal was skipped")
            with contextlib.suppress(docker.errors.NotFound):
                container.remove()

    def start(
        self, task: dict, session_id: str, directory: Path, *, ca_directory: Path | None = None
    ) -> dict:
        policy = _policy(task)
        connection_settings, auth = _proxy_settings()
        identity = ca_info(ca_directory) if ca_directory is not None else None
        image = self._image()
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        if ca_directory is None:
            (directory / "ca").mkdir(mode=0o700, exist_ok=True)
        else:
            ca_directory = Path(ca_directory).resolve()
        _atomic_json(directory / "scope.json", policy)
        metadata = self._metadata(str(task["id"]), session_id, "capture")
        metadata.update(directory=str(directory), proxy_port=None, ca_ready=False, **connection_settings)
        if identity is not None:
            metadata.update(ca_directory=str(ca_directory), ca_shared=True, ca_sha256=identity["sha256"])
        options = self._options()
        options["environment"].update(MCP_CAPTURE_DIR="/capture", MCP_CAPTURE_SESSION=session_id)
        if auth:
            options["environment"]["MCP_CAPTURE_PROXY_AUTH_REQUIRED"] = "1"
        volumes = {
            str(directory): {"bind": "/capture", "mode": "rw"},
            str(Path(__file__).parent.resolve()): {"bind": "/opt/traffic", "mode": "ro"},
        }
        if ca_directory is not None:
            volumes[str(ca_directory)] = {"bind": "/ca", "mode": "ro"}
        container = None
        try:
            container = self.client.containers.create(
                image=image,
                entrypoint=["mitmdump"],
                command=[
                    "--mode",
                    "regular",
                    "--listen-host",
                    "0.0.0.0",
                    "--listen-port",
                    "8080",
                    "--set",
                    "confdir=/ca" if ca_directory is not None else "confdir=/capture/ca",
                    "--set",
                    "connection_strategy=lazy",
                    "--set",
                    "block_global=false",
                    "--set",
                    "flow_detail=0",
                    "--quiet",
                    "-s",
                    "/opt/traffic/capture_addon.py",
                ]
                + (["--proxyauth", auth] if auth else []),
                name=f"strixops-mcp-capture-{metadata['owner_token'][:16]}",
                labels=self._labels(metadata),
                ports={"8080/tcp": (metadata["proxy_bind_host"], None)},
                volumes=volumes,
                **options,
            )
            metadata["container_id"] = container.id
            _atomic_json(directory / "runtime.json", metadata)
            container.start()
            deadline = time.monotonic() + START_TIMEOUT
            while time.monotonic() < deadline:
                container = self._owned(task["id"], metadata)
                if container is None or not container.attrs.get("State", {}).get("Running"):
                    raise RuntimeUnavailable("Capture worker exited before it became ready")
                bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {}).get("8080/tcp") or []
                if bindings:
                    binding = bindings[0]
                    if not _binding_matches(bindings, metadata["proxy_bind_host"]):
                        raise RuntimeUnavailable(
                            "Capture listener does not match the configured bind address"
                        )
                    metadata["proxy_port"] = int(binding["HostPort"])
                if metadata["proxy_port"] and self.ca_path(directory, ca_directory=ca_directory).is_file():
                    try:
                        with socket.create_connection(
                            (_probe_host(metadata["proxy_bind_host"]), metadata["proxy_port"]), timeout=0.3
                        ) as connection:
                            # Docker's published TCP port can accept before
                            # mitmdump has bound its listener. A hostless request
                            # proves proxy readiness without contacting a target.
                            auth_header = (
                                b"Proxy-Authorization: Basic "
                                + base64.b64encode(auth.encode("ascii"))
                                + b"\r\n"
                                if auth
                                else b""
                            )
                            connection.sendall(
                                b"GET / HTTP/1.1\r\n" + auth_header + b"Connection: close\r\n\r\n"
                            )
                            response = connection.recv(512)
                            if not response.startswith(b"HTTP/"):
                                raise OSError("Capture proxy has not started responding")
                            if response.split(b" ", 2)[1:2] == [b"407"]:
                                raise RuntimeUnavailable("Capture proxy authentication configuration failed")
                        metadata.update(
                            ca_ready=True, runtime_status="running", scope_revision=policy["scope_revision"]
                        )
                        _atomic_json(directory / "runtime.json", metadata)
                        return metadata
                    except OSError:
                        pass
                time.sleep(0.2)
            raise RuntimeUnavailable("Capture proxy did not become ready within 20 seconds")
        except BaseException as exc:
            if container is not None and metadata.get("container_id"):
                with contextlib.suppress(Exception):
                    self._remove(task["id"], metadata)
            if isinstance(exc, docker.errors.DockerException):
                # Docker errors can echo the container command and its proxyauth
                # argument. Never copy daemon errors into task/session records.
                raise RuntimeUnavailable(
                    "Cannot start the MCP capture container; check Docker configuration"
                ) from None
            raise

    def stop(self, task_id: str, session: dict) -> None:
        self._remove(task_id, session)

    def status(self, task_id: str, session: dict) -> dict:
        try:
            container = self._owned(task_id, session)
            running = bool(container and container.attrs.get("State", {}).get("Running"))
            result = {
                "runtime_status": "running" if running else "stopped",
                "running": running,
                "ca_ready": self.ca_path(
                    Path(session["directory"]), ca_directory=session.get("ca_directory")
                ).is_file(),
            }
            # Return recovered identity to the private service store. Its public
            # projection removes owner_token and directory.
            result.update(
                {
                    key: session[key]
                    for key in (
                        "container_id",
                        "owner_token",
                        "session_id",
                        "task_id",
                        "proxy_host",
                        "proxy_bind_host",
                        "proxy_auth_required",
                        "proxy_port",
                        "ca_directory",
                        "ca_shared",
                        "ca_sha256",
                    )
                    if key in session
                }
            )
            if running:
                bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {}).get("8080/tcp") or []
                bind_host = session.get("proxy_bind_host", "127.0.0.1")
                if bindings and not _binding_matches(bindings, bind_host):
                    raise ValueError("Capture listener does not match its saved bind address")
                if not ipaddress.ip_address(bind_host).is_loopback and not session.get("proxy_auth_required"):
                    raise ValueError("Remote capture listener has no proxy authentication record")
                if bindings:
                    result.update(
                        proxy_host=session.get("proxy_host", bind_host),
                        proxy_bind_host=bind_host,
                        proxy_auth_required=bool(session.get("proxy_auth_required")),
                        proxy_port=int(bindings[0]["HostPort"]),
                    )
                if not result["ca_ready"] or not bindings:
                    result["runtime_status"] = "starting"
            status_path = Path(session["directory"]) / "capture-status.json"
            if status_path.exists() and status_path.stat().st_size < 65536:
                result["capture_status"] = json.loads(status_path.read_text())
            return result
        except (docker.errors.DockerException, RuntimeUnavailable, ValueError, KeyError, OSError) as exc:
            return {"runtime_status": "unavailable", "running": False, "error": str(exc), "ca_ready": False}

    def update_scope(self, task: dict, session: dict, directory: Path) -> None:
        if Path(directory).resolve() != Path(session["directory"]).resolve():
            raise ValueError("Capture session directory mismatch")
        self._owned(task["id"], session)
        _atomic_json(Path(directory) / "scope.json", _policy(task))

    def replay(
        self, task: dict, flow: dict, modifications: dict | None = None, test_id: str = "", cancel: Any = None
    ) -> dict:
        request = prepare_request(task, flow, modifications)
        image = self._image()
        if _cancelled(cancel):
            raise RuntimeError("Replay cancelled")
        metadata = self._metadata(str(task["id"]), uuid.uuid4().hex, "replay")
        with tempfile.TemporaryDirectory(prefix="strixops-mcp-replay-") as temporary:
            directory = Path(temporary)
            _atomic_json(
                directory / "input.json",
                {
                    "task": _policy(task),
                    "request": request,
                    "session_id": metadata["session_id"],
                    "test_id": test_id,
                },
            )
            container = None
            try:
                container = self.client.containers.create(
                    image=image,
                    auto_remove=True,
                    entrypoint=["python"],
                    command=["/opt/traffic/http_worker.py", "/work/input.json", "/work/output.json"],
                    name=f"strixops-mcp-replay-{metadata['owner_token'][:16]}",
                    labels=self._labels(metadata),
                    volumes={
                        str(directory): {"bind": "/work", "mode": "rw"},
                        str(Path(__file__).parent.resolve()): {"bind": "/opt/traffic", "mode": "ro"},
                    },
                    **self._options(),
                )
                metadata["container_id"] = container.id
                container.start()
                deadline = time.monotonic() + REPLAY_TIMEOUT
                while time.monotonic() < deadline:
                    if _cancelled(cancel):
                        raise RuntimeError("Replay cancelled")
                    container = self._owned(task["id"], metadata, role="replay")
                    if container is None or not container.attrs.get("State", {}).get("Running"):
                        output = directory / "output.json"
                        if (
                            container is not None
                            and container.attrs["State"].get("ExitCode") != 0
                            or not output.is_file()
                        ):
                            raise RuntimeUnavailable("Replay worker failed before writing a result")
                        if output.stat().st_size > 3 * 1024 * 1024:
                            raise RuntimeUnavailable("Replay result exceeds the evidence size limit")
                        result = json.loads(output.read_text())
                        if not isinstance(result, dict) or not result.get("id") or not result.get("request"):
                            raise RuntimeUnavailable("Replay worker returned an invalid result")
                        result["parent_flow_id"] = flow.get("id")
                        return result
                    time.sleep(0.2)
                raise RuntimeUnavailable("Replay exceeded its 40 second runtime limit")
            finally:
                if metadata.get("container_id"):
                    self._remove(task["id"], metadata, role="replay")
