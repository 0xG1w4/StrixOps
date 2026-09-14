"""Inspect persisted MCP capture identities without importing application services.

This helper runs in the Console's Python environment, so Docker uses the same
SDK environment configuration. It never changes containers or stored records.
Only a fixed boolean result crosses the subprocess boundary.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from typing import Any

OWNER_LABEL = "io.strixops.mcp.owner"
TASK_LABEL = "io.strixops.mcp.task"
SESSION_LABEL = "io.strixops.mcp.session"
ROLE_LABEL = "io.strixops.mcp.role"
_INPUT_LIMIT = 1024 * 1024


def _identity(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value) is not None


def _expected_sessions(sessions: Any) -> dict[str, dict[str, str]] | None:
    if not isinstance(sessions, list) or not sessions:
        return None
    expected = {}
    seen = set()
    for session in sessions:
        if not isinstance(session, dict):
            return None
        if not all(_identity(session.get(key)) for key in ("id", "task_id", "owner_token")):
            return None
        if session.get("session_id", session["id"]) != session["id"]:
            return None
        container_id = session.get("container_id")
        if not isinstance(container_id, str) or not re.fullmatch(r"[a-f0-9]{64}", container_id):
            return None
        identity = (session["task_id"], session["id"])
        if container_id in expected or identity in seen:
            return None
        seen.add(identity)
        expected[container_id] = {
            OWNER_LABEL: session["owner_token"],
            TASK_LABEL: session["task_id"],
            SESSION_LABEL: session["id"],
            ROLE_LABEL: "capture",
        }
    return expected


def _inactive_container(container: Any, task_id: str, expected: dict[str, dict[str, str]]) -> bool:
    attributes = container.attrs
    if not isinstance(attributes, dict):
        return False
    container_id = container.id
    if (
        not isinstance(container_id, str)
        or not re.fullmatch(r"[a-f0-9]{64}", container_id)
        or attributes.get("Id") != container_id
    ):
        return False
    config = attributes.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or labels.get(TASK_LABEL) != task_id:
        return False
    if (
        not all(_identity(labels.get(key)) for key in (OWNER_LABEL, TASK_LABEL, SESSION_LABEL))
        or labels.get(ROLE_LABEL) not in {"capture", "replay"}
    ):
        return False
    if container_id in expected and any(
        labels.get(key) != value for key, value in expected[container_id].items()
    ):
        return False
    state = attributes.get("State")
    if (
        not isinstance(state, dict)
        or state.get("Status") not in {"exited", "dead"}
        or any(state.get(key) is not False for key in ("Running", "Paused", "Restarting"))
    ):
        return False
    host_config = attributes.get("HostConfig")
    policy = host_config.get("RestartPolicy") if isinstance(host_config, dict) else None
    return isinstance(policy, dict) and policy.get("Name") in {"", "no"}


def captures_inactive(sessions: list[dict], client: Any = None) -> bool:
    """Prove captures and other containers for their tasks are inactive.

    Missing saved containers are acceptable only after a successful task-label
    inventory. Unavailable Docker, unknown state, restart policies, or mismatched
    identities leave the saved activity blocking maintenance.
    """
    expected = _expected_sessions(sessions)
    if expected is None:
        return False
    owns_client = client is None
    try:
        import docker

        if owns_client:
            client = docker.from_env(timeout=2)
        for container_id, labels in expected.items():
            try:
                container = client.containers.get(container_id)
                container.reload()
            except docker.errors.NotFound:
                continue
            if container.id != container_id or not _inactive_container(
                container, labels[TASK_LABEL], expected
            ):
                return False
        for task_id in sorted({labels[TASK_LABEL] for labels in expected.values()}):
            containers = client.containers.list(all=True, filters={"label": f"{TASK_LABEL}={task_id}"})
            if not isinstance(containers, list):
                return False
            for container in containers:
                try:
                    container.reload()
                except docker.errors.NotFound:
                    continue
                if not _inactive_container(container, task_id, expected):
                    return False
        return True
    except Exception:
        return False
    finally:
        if owns_client and client is not None:
            with contextlib.suppress(Exception):
                client.close()


def main() -> None:
    inactive = False
    try:
        raw = sys.stdin.buffer.read(_INPUT_LIMIT + 1)
        if len(raw) <= _INPUT_LIMIT:
            sessions = json.loads(raw)
            inactive = captures_inactive(sessions)
    except Exception:
        pass
    print(json.dumps({"inactive": inactive is True}))


if __name__ == "__main__":
    main()
