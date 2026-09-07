"""Persist the exact run/container association for read-only proxy telemetry."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger("strixops.proxy_status")


def record_caido_runtime(run_dir: Path, container_id: str) -> bool:
    """Keep only the Docker identity; telemetry must never guess a run's container."""
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        return False
    temporary: Path | None = None
    try:
        directory = run_dir / ".state"
        directory.mkdir(parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(prefix=".caido-runtime-", dir=directory)
        temporary = Path(filename)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"container_id": container_id, "run_name": run_dir.name}, stream)
        os.replace(temporary, directory / "caido-runtime.json")
        return True
    except OSError as exc:
        logger.warning("Could not save proxy runtime identity (%s)", type(exc).__name__)
        return False
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
