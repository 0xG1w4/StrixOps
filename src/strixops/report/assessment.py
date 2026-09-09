"""Run-owned, attributed assessment records; no process-global or upstream state.

Coverage is an agent's account of reviewed surfaces. Resolving every recorded
row does not establish exhaustive coverage, and execution completion never
changes an unresolved assessment into a clean result.
"""

from __future__ import annotations

import copy
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

OUTCOMES = ("reported", "no_issue_found", "ruled_out", "not_applicable", "needs_follow_up")
REQUIRES_EVIDENCE = frozenset({"ruled_out", "not_applicable", "needs_follow_up"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value.strip()


def _conclusion(outcome: str, evidence: str) -> tuple[str, str]:
    outcome = outcome.strip().lower()
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {'|'.join(OUTCOMES)}")
    evidence = evidence.strip()
    if outcome in REQUIRES_EVIDENCE and not evidence:
        raise ValueError(f"evidence is required for {outcome}")
    return outcome, evidence


class AssessmentState:
    """Mutable ledger owned by one RunState and protected by its shared lock."""

    def __init__(self, save: Callable[[], None], lock: Any) -> None:
        self._save = save
        self._lock = lock
        self._coverage: dict[str, dict[str, Any]] = {}
        self._models: dict[str, dict[str, Any]] = {}

    def _commit(self, collection: dict, key: str, value: dict) -> None:
        old = collection.get(key)
        collection[key] = value
        try:
            self._save()
        except Exception:
            if old is None:
                del collection[key]
            else:
                collection[key] = old
            raise

    def record_coverage(
        self,
        *,
        surface: str,
        risk_area: str,
        outcome: str,
        evidence: str,
        agent_id: str,
        agent_name: str,
    ) -> dict[str, Any]:
        surface, risk_area = _text(surface, "surface"), _text(risk_area, "risk_area")
        outcome, evidence = _conclusion(outcome, evidence)
        with self._lock:
            for entry in self._coverage.values():
                # URL paths and repository paths can be case-sensitive.
                if entry["surface"] == surface and entry["risk_area"].casefold() == risk_area.casefold():
                    return {
                        "success": False,
                        "entry_id": entry["id"],
                        "error": "This surface/risk already exists; use update_coverage.",
                    }
            entry_id = f"cov-{len(self._coverage) + 1:04d}"
            now = _now()
            entry = {
                "id": entry_id,
                "surface": surface,
                "risk_area": risk_area,
                "outcome": outcome,
                "evidence": evidence,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "created_by": agent_id,
                "created_by_name": agent_name,
                "created_at": now,
                "updated_at": now,
                "history": [],
            }
            self._commit(self._coverage, entry_id, entry)
            return {"success": True, "entry_id": entry_id, "entry": copy.deepcopy(entry)}

    def update_coverage(
        self,
        *,
        entry_id: str,
        outcome: str,
        evidence: str,
        agent_id: str,
        agent_name: str,
    ) -> dict[str, Any]:
        outcome, evidence = _conclusion(outcome, evidence)
        with self._lock:
            old = self._coverage.get(entry_id)
            if old is None:
                return {"success": False, "error": f"Coverage entry {entry_id!r} not found"}
            if (old["outcome"], old["evidence"]) == (outcome, evidence):
                return {"success": True, "entry_id": entry_id, "changed": False}
            entry = copy.deepcopy(old)
            entry["history"].append(
                {
                    key: copy.deepcopy(old[key])
                    for key in (
                        "outcome",
                        "evidence",
                        "agent_id",
                        "agent_name",
                        "updated_at",
                    )
                }
            )
            entry.update(
                outcome=outcome,
                evidence=evidence,
                agent_id=agent_id,
                agent_name=agent_name,
                updated_at=_now(),
            )
            self._commit(self._coverage, entry_id, entry)
            return {"success": True, "entry_id": entry_id, "changed": True, "entry": copy.deepcopy(entry)}

    def list_coverage(self, *, outcome: str | None = None, surface: str | None = None) -> dict:
        if outcome is not None:
            outcome = outcome.strip().lower()
            if outcome not in OUTCOMES:
                raise ValueError(f"outcome must be one of {'|'.join(OUTCOMES)}")
        with self._lock:
            entries = list(self._coverage.values())
            counts = dict.fromkeys(OUTCOMES, 0) | dict(Counter(e["outcome"] for e in entries))
            filtered = [
                e
                for e in entries
                if (outcome is None or e["outcome"] == outcome)
                and (not surface or surface.casefold() in e["surface"].casefold())
            ]
            return {
                "success": True,
                "entries": copy.deepcopy(filtered),
                "total": len(entries),
                "outcome_counts": counts,
            }

    def save_threat_model(
        self,
        *,
        target: str,
        content: str,
        agent_id: str,
        agent_name: str,
    ) -> dict:
        target, content = _text(target, "target"), _text(content, "content")
        with self._lock:
            previous = self._models.get(target)
            history = copy.deepcopy(previous["history"]) if previous else []
            if previous:
                history.append({k: copy.deepcopy(v) for k, v in previous.items() if k != "history"})
            model = {
                "target": target,
                "content": content,
                "written_by": agent_id,
                "written_by_name": agent_name,
                "updated_at": _now(),
                "revision": previous["revision"] + 1 if previous else 1,
                "amendments": [],
                "history": history,
            }
            self._commit(self._models, target, model)
            return {"success": True, "target": target, "revision": model["revision"]}

    def get_threat_model(self, *, target: str) -> dict:
        target = _text(target, "target")
        with self._lock:
            model = self._models.get(target)
            if model is None:
                return {
                    "success": True,
                    "found": False,
                    "target": target,
                    "message": "No model recorded for this target in this run; derive and save one.",
                }
            return {"success": True, "found": True, **copy.deepcopy(model)}

    def amend_threat_model(
        self,
        *,
        target: str,
        addendum: str,
        agent_id: str,
        agent_name: str,
    ) -> dict:
        target, addendum = _text(target, "target"), _text(addendum, "addendum")
        with self._lock:
            previous = self._models.get(target)
            if previous is None:
                return {"success": False, "error": "No base model exists; use save_threat_model first."}
            model = copy.deepcopy(previous)
            now = _now()
            model["amendments"].append(
                {"content": addendum, "agent_id": agent_id, "agent_name": agent_name, "timestamp": now}
            )
            model.update(updated_at=now, revision=previous["revision"] + 1)
            self._commit(self._models, target, model)
            return {
                "success": True,
                "target": target,
                "revision": model["revision"],
                "amendment_count": len(model["amendments"]),
            }

    def snapshot(self, lifecycle_status: str) -> dict[str, Any]:
        with self._lock:
            coverage = self.list_coverage()
            entries = coverage["entries"]
            unresolved = coverage["outcome_counts"]["needs_follow_up"]
            return {
                "schema_version": 1,
                "generated_at": _now(),
                "lifecycle": {"status": lifecycle_status},
                "interpretation": "Execution completion is not proof of exhaustive coverage or safety. "
                "Coverage and threat models are agent-reported assessments.",
                "coverage": {
                    "status": "recorded" if entries else "unknown",
                    "provenance": "agent_reported",
                    "entries": entries,
                    "outcome_counts": coverage["outcome_counts"],
                    "unresolved_count": unresolved if entries else None,
                    "completeness": (
                        "unknown"
                        if not entries
                        else "has_unresolved"
                        if unresolved
                        else "recorded_items_resolved"
                    ),
                },
                "threat_models": {
                    "status": "recorded" if self._models else "unknown",
                    "models": copy.deepcopy(list(self._models.values())),
                },
            }


def summary(document: dict[str, Any]) -> dict[str, Any]:
    coverage, models = document["coverage"], document["threat_models"]
    return {
        "schema_version": 1,
        "path": "assessment.json",
        "status": "recorded" if coverage["entries"] or models["models"] else "unknown",
        "coverage_entry_count": len(coverage["entries"]),
        "unresolved_count": coverage["unresolved_count"],
        "threat_model_count": len(models["models"]),
    }


def empty_assessment(status: str = "unknown") -> dict[str, Any]:
    """Represent legacy/missing assessment data without inventing clean results."""
    return AssessmentState(lambda: None, threading.RLock()).snapshot(status)
