"""Current notebook records shared by automatic and saved-run report synthesis."""

from __future__ import annotations

import copy
from typing import Any

from strixops.report.credential_csv import load_credential_csv
from strixops.report.credential_store import CredentialStore
from strixops.report.credentials import (
    collect_credentials,
    credential_source_warnings,
    merge_credential_inventory,
)
from strixops.report.notes import NotesStore


def notebook_context(run_state: Any) -> dict[str, Any]:
    """Keep working assessments attributed, without deleted or superseded material."""
    frozen = getattr(run_state, "report_notebook", None)
    if frozen is not None:
        return copy.deepcopy(frozen)
    omissions: list[dict[str, str]] = []
    snapshot = getattr(run_state, "report_notes", None)
    if snapshot is None:
        store = getattr(run_state, "notes", None) or NotesStore(run_state.run_dir)
        snapshot = store.snapshot()
    notes: list[dict[str, Any]] = []
    if isinstance(snapshot, dict) and snapshot.get("success") is True:
        notes = copy.deepcopy(snapshot.get("notes") or [])
    else:
        omissions.append({"source": "shared_notes", "reason": "source_unreadable"})

    try:
        assessment = run_state.assessment.snapshot(str(run_state.run_record.get("status") or "unknown"))
        if not isinstance(assessment, dict):
            raise ValueError("invalid assessment")
    except Exception:
        assessment = {}
        omissions.append({"source": "assessment", "reason": "source_unreadable"})
    groups: dict[str, list[dict[str, Any]]] = {}
    for key, field in (("coverage", "entries"), ("threat_models", "models")):
        section = assessment.get(key) or {}
        entries = section.get(field, []) if isinstance(section, dict) else None
        if not isinstance(entries, list) or not all(isinstance(row, dict) for row in entries):
            omissions.append({"source": key, "reason": "source_unreadable"})
            entries = []
        groups[key] = [
            {name: copy.deepcopy(value) for name, value in row.items() if name != "history"}
            for row in entries
        ]
    # The collector sees the same current notes and assessments as the composer,
    # not history, model credentials or arbitrary saved attachment bodies.
    current_assessment = {
        "coverage": {"entries": groups["coverage"]},
        "threat_models": {"models": groups["threat_models"]},
    }
    try:
        credentials = collect_credentials(
            reports=run_state.reports,
            internal_findings=run_state.internal_findings,
            notes=notes,
            assessment=current_assessment,
            final_fields=run_state.final_fields,
            campaign=run_state.run_record.get("internal_campaign"),
        )
    except Exception:
        credentials = []
        omissions.append({"source": "credentials", "reason": "source_unreadable"})
    csv_credentials: list[dict[str, Any]] = []
    try:
        csv_credentials, csv_warnings = load_credential_csv(
            run_dir=run_state.run_dir,
            manifest=run_state.run_record.get("evidence"),
            reports=run_state.reports,
            internal_findings=run_state.internal_findings,
        )
        omissions.extend({"source": "credential_csv", "reason": code} for code in csv_warnings)
    except Exception:
        omissions.append({"source": "credential_csv", "reason": "source_unreadable"})
    registered: list[dict[str, Any]] = []
    try:
        register = getattr(run_state, "credentials", None) or CredentialStore(run_state.run_dir)
        inventory = register.snapshot()
        if inventory.get("success") is not True:
            raise ValueError("credential register unavailable")
        registered = inventory["credentials"]
    except Exception:
        omissions.append({"source": "credential_register", "reason": "source_unreadable"})
    credentials = merge_credential_inventory(
        registered, credentials, csv_credentials,
        reports=run_state.reports, internal_findings=run_state.internal_findings,
    )
    omissions.extend(
        {"source": "credentials", "reason": code}
        for code in credential_source_warnings(
            reports=run_state.reports,
            internal_findings=run_state.internal_findings,
            credentials=credentials,
        )
    )
    return {"notes": notes, **groups, "credentials": credentials, "omissions": omissions}
