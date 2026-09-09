"""Run-local prompt and skill snapshot shared by root, children and lazy loads."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from strixops import skills
from strixops.agents import prompts
from strixops.engine.scanconfig import ScanSpec

_ACTIVE: ContextVar[PromptResources | None] = ContextVar("strixops_prompt_resources", default=None)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


@dataclass(frozen=True)
class PromptResources:
    run_dir: Path
    parts: dict[str, str]
    skill_records: list[dict]
    spec: ScanSpec | None

    @classmethod
    def for_run(cls, run_dir: Path, spec: ScanSpec | None) -> PromptResources:
        run_dir = Path(run_dir).resolve()
        current = _ACTIVE.get()
        if current is not None and current.run_dir == run_dir:
            return current
        path = run_dir / ".state" / "prompt_resources.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema_version") != 1:
                raise ValueError("Unsupported prompt resource snapshot")
            resources = cls(
                run_dir,
                data["prompt_parts"],
                data["skills"],
                ScanSpec(**data["spec"]) if data["spec"] is not None else None,
            )
        else:
            # No await in capture or publish: every child on this run's event
            # loop observes the same snapshot, even after Console edits.
            resources = cls(
                run_dir,
                {
                    path.name: path.read_text(encoding="utf-8")
                    for path in sorted(prompts.PROMPT_PARTS_DIR.glob("*.md"))
                },
                skills.snapshot_skills(),
                ScanSpec(**asdict(spec)) if spec is not None else None,
            )
            _write_json(
                path,
                {
                    "schema_version": 1,
                    "prompt_parts": resources.parts,
                    "skills": resources.skill_records,
                    "spec": asdict(resources.spec) if resources.spec is not None else None,
                },
            )
            _write_json(
                run_dir / ".state" / "prompt_manifest.json",
                {
                    "schema_version": 1,
                    "resources_file": "prompt_resources.json",
                    "resources_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "prompt_parts": {name: _sha(content) for name, content in resources.parts.items()},
                    "skills": {row["id"]: _sha(row["content"]) for row in resources.skill_records},
                    "agents": [],
                },
            )
        return resources

    @contextmanager
    def activate(self):
        token = _ACTIVE.set(self)
        try:
            with prompts.use_prompt_parts(self.parts), skills.use_skill_snapshot(self.skill_records):
                yield self
        finally:
            _ACTIVE.reset(token)

    def record_prompt(self, agent_id: str, name: str, prompt: str, tools: list[Any]) -> None:
        # IDs, rather than display names, keep identically named siblings apart.
        safe_id = "".join(char for char in agent_id if char.isalnum() or char in "_-")[:64]
        if not safe_id:
            safe_id = _sha(name)[:16]
        filename = f"prompt_{safe_id}.md"
        state_dir = self.run_dir / ".state"
        (state_dir / filename).write_text(prompt, encoding="utf-8")
        path = state_dir / "prompt_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["agents"].append(
            {
                "agent_id": agent_id,
                "name": name,
                "prompt_file": filename,
                "prompt_sha256": _sha(prompt),
                "tool_names": [tool.name for tool in tools],
            }
        )
        _write_json(path, manifest)
