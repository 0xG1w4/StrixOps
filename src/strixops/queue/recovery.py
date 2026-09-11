"""Recover only resources proved to belong to an exited scan process."""

from __future__ import annotations

import contextlib
import re

from .processes import process_alive

OWNER_LABEL = "io.strixops.run-owner"


def recover_resources(lease: dict) -> bool:
    if process_alive(lease.get("pid"), lease.get("start_identity") or "") is not False:
        return False
    if lease.get("resource_phase") == "not_started":
        return True
    owner, daemon = lease.get("resource_owner", ""), lease.get("resource_daemon", "")
    if not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{32}", owner) or not daemon:
        return False
    client = None
    try:
        import docker

        client = docker.from_env(timeout=10)
        if client.info().get("ID") != daemon:
            return False
        containers = client.containers.list(all=True, filters={"label": f"{OWNER_LABEL}={owner}"})
        for container in containers:
            container.reload()
            if (container.attrs.get("Config", {}).get("Labels") or {}).get(OWNER_LABEL) != owner:
                return False
            if process_alive(lease.get("pid"), lease.get("start_identity") or "") is not False:
                return False
            container.remove(force=True, v=True)
        return not client.containers.list(all=True, filters={"label": f"{OWNER_LABEL}={owner}"})
    except Exception:
        return False
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
