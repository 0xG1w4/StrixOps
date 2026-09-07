"""Run-directory naming and placement.

Contract (verified against ``apps/api/services/run_resolver.py`` and
``apps/api/services/supervisor.py``):

* The platform binds a spawned engine to a run dir by watching
  ``$STRIX_RUNS`` for a new directory whose name matches
  ``^.+_[0-9a-fA-F]{4}$`` and whose prefix matches one of the candidate
  prefixes derived from the task's safe name / target (see
  ``candidate_run_prefixes`` in the resolver: variants with ``_``→``-``,
  ``_``→``.``, ``https://`` forms, and a lowercase slug truncated to 32).
* Generation mirrors the reference implementation's
  ``generate_run_name``/``_slugify_for_run_name``: label from the target
  (URL netloc for web scans, the raw string otherwise), slugified to
  ``[a-z0-9-]`` ≤32 chars, suffixed with ``token_hex(2)``.
* The run dir plus an existing (possibly empty) ``events.jsonl`` must appear
  within 60 s of spawn; callers must create both before any slow work.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

FALLBACK_SLUG = "pentest"
SLUG_MAX_LENGTH = 32


def slugify_for_run_name(text: str, max_length: int = SLUG_MAX_LENGTH) -> str:
    """Lowercase, non-alphanumerics collapsed to ``-``, trimmed, ≤ max_length."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or FALLBACK_SLUG


def derive_target_label(target: str, scan_type: str) -> str:
    """Human label for a target: URL netloc for web scans, the raw string otherwise."""
    text = (target or "").strip()
    if not text:
        return FALLBACK_SLUG
    if scan_type == "web":
        try:
            parsed = urlparse(text if "://" in text else f"https://{text}")
            return str(parsed.netloc or parsed.path or text)
        except Exception:
            return text
    return text


def generate_run_name(target: str, scan_type: str) -> str:
    """``<slug>_<4-hex>`` — the naming scheme the platform's resolver matches.

    ``STRIXOPS_RUN_NAME`` (when set, e.g. by the console) overrides the
    generated name; the platform never sets it.
    """
    override = (__import__("os").environ.get("STRIXOPS_RUN_NAME") or "").strip()
    if override:
        return override
    label = derive_target_label(target, scan_type)
    return f"{slugify_for_run_name(label)}_{secrets.token_hex(2)}"


def resolve_runs_root(runs_root: str | None = None) -> Path:
    """Runs root directory.

    The platform always passes ``STRIX_RUNS`` in the spawn environment; the
    cwd fallback only exists for manual runs.
    """
    raw = (runs_root or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.cwd() / "strix_runs"


def create_run_dir(runs_root: Path, run_name: str) -> Path:
    """Create the run dir, its ``.state`` subdir, and an empty ``events.jsonl``.

    Returns the run dir path. Everything here is fast and local so callers can
    invoke it immediately after spawn, well inside the platform's 60 s
    detection window and before any Docker work.
    """
    run_dir = runs_root / run_name
    (run_dir / ".state").mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").touch(exist_ok=True)
    return run_dir
