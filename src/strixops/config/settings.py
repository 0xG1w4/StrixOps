"""Environment-driven engine settings.

The platform passes everything the engine needs through the spawn
environment (``apps/api/services/supervisor.py:_build_strix_env``); nothing
is read from config files. ``STRIX_RUNS`` is honored directly — unlike the
reference implementation, which only ever wrote to ``<cwd>/strix_runs`` and
relied on deployment-time bridging.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from strixops.config.model_options import validate_model_options


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@dataclass(frozen=True)
class EngineSettings:
    llm_api_base: str
    llm_api_key: str
    strix_llm: str
    strix_runs: str
    operator_hints_dir: str
    host_workspace_dir: str
    dry_run: bool
    llm_api_mode: str = "chat_completions"
    llm_reasoning_effort: str = "default"

    @classmethod
    def from_env(cls) -> EngineSettings:
        return cls(
            llm_api_base=_env("LLM_API_BASE"),
            llm_api_key=_env("LLM_API_KEY"),
            strix_llm=_env("STRIX_LLM"),
            strix_runs=_env("STRIX_RUNS"),
            operator_hints_dir=_env("STRIX_OPERATOR_HINTS_DIR"),
            host_workspace_dir=_env("STRIX_HOST_WORKSPACE_DIR"),
            dry_run=_env("STRIXOPS_DRY_RUN").lower() in {"1", "true", "yes", "on"},
            llm_api_mode=_env("LLM_API_MODE").lower() or "chat_completions",
            llm_reasoning_effort=_env("LLM_REASONING_EFFORT").lower() or "default",
        )

    def validate(self) -> list[str]:
        """Preflight problems; empty list means good to run."""
        problems: list[str] = []
        if not self.dry_run:
            if not self.llm_api_base:
                problems.append("LLM_API_BASE is empty")
            if not self.llm_api_key:
                problems.append("LLM_API_KEY is empty")
            if not self.strix_llm:
                problems.append("STRIX_LLM is empty")
            problems.extend(
                validate_model_options(self.strix_llm, self.llm_api_mode, self.llm_reasoning_effort)
            )
        return problems

    @property
    def host_workspace_path(self) -> Path | None:
        return Path(self.host_workspace_dir).expanduser() if self.host_workspace_dir else None
