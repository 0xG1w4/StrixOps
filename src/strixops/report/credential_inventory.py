"""Bounded views over the full register and smaller legacy observations."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

from strixops.report.credential_store import _ID, _LIMITS, VALIDATION_STATUSES, CredentialStore
from strixops.report.credentials import (
    _credential_references,
    credential_source_warnings,
    merge_credential_inventory,
    merge_credentials,
)

REPORT_CREDENTIAL_LIMIT = 100


def matches(row: dict, query: str | None, validation_status: str | None) -> bool:
    if validation_status and row.get("validation_status") != validation_status:
        return False
    return not query or query.casefold() in "\n".join(str(row.get(key, "")) for key in _LIMITS).casefold()


def summary(rows: list[dict], base: dict | None = None) -> dict:
    result = {}
    for field in ("validation_status", "secret_type", "severity"):
        counts = Counter((base or {}).get(field, {}))
        if field == "validation_status":
            counts.update({status: 0 for status in VALIDATION_STATUSES})
        counts.update(row.get(field, "unknown") for row in rows)
        result[field] = dict(counts)
    return result


def report_priority(row: dict) -> tuple:
    return (
        row.get("validation_status") != "validated",
        -{"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}.get(row.get("severity"), -1),
        row.get("id", ""),
    )


class CredentialInventory:
    """Query the database without materializing it to merge finding provenance.

    Only registered IDs mentioned by bounded legacy observations are overlaid.
    The remaining database rows retain SQL pagination and a streaming iterator.
    """

    def __init__(
        self, store: CredentialStore, supplemental: list[dict], *,
        reports: list[dict], internal_findings: list[dict], datasets: list[dict] | None = None,
    ) -> None:
        self.store = store
        self.warnings: list[str] = []
        self.datasets = datasets or []
        self.reference_sources: dict[str, list[dict]] = {}
        supplemental = merge_credentials(supplemental)
        ids = {row["id"] for row in supplemental}
        dataset_refs: set[str] = set()
        for kind, records in (("vulnerability", reports), ("finding", internal_findings)):
            for record in records:
                if not isinstance(record, dict):
                    continue
                provenance = {
                    "kind": kind, "id": str(record.get("id") or ""),
                    "title": str(record.get("title") or record.get("id") or ""),
                }
                for reference in _credential_references(record):
                    if _ID.fullmatch(reference):
                        self.reference_sources.setdefault(reference, []).append(provenance)
                metadata = record.get("metadata")
                if isinstance(metadata, dict) and isinstance(metadata.get("credential_dataset_ids"), list):
                    dataset_refs.update(
                        value for value in metadata["credential_dataset_ids"] if isinstance(value, str)
                    )
        valid_dataset_refs = {value for value in dataset_refs if re.fullmatch(r"dataset-[0-9a-f]{32}", value)}
        if valid_dataset_refs:
            matched = store.get_datasets(ids=valid_dataset_refs)
            if matched.get("success"):
                self.datasets = list({row["id"]: row for row in [
                    *matched["datasets"], *self.datasets,
                ]}.values())
        ids = {value for value in ids if _ID.fullmatch(value)}
        registered = store.get_credentials(ids)
        self.available = registered.get("success") is True
        self.source_status = registered.get("source_status", "missing")
        if not self.available:
            self.warnings.append("credential_register_unreadable")
        current = registered.get("credentials", []) if self.available else []
        self.overlay = [self._attributed(row) for row in merge_credential_inventory(current, supplemental)]
        self.overlay.sort(key=lambda row: row["id"])
        # A concurrent import may register a supplemental identity after this
        # snapshot. Keep it excluded from the base query to avoid double counts.
        self.exclude_ids = tuple(row["id"] for row in self.overlay)
        existing = store.existing_credential_ids(self.reference_sources) if self.reference_sources else {}
        if self.reference_sources and not existing.get("success"):
            self.warnings.append("credential_register_unreadable")
        self.warnings.extend(credential_source_warnings(
            reports=reports, internal_findings=internal_findings, credentials=self.overlay,
            dataset_ids={row["id"] for row in self.datasets},
            known_credential_ids=set(existing.get("credential_ids", [])),
        ))

    def _attributed(self, row: dict) -> dict:
        sources = list(row.get("sources", []))
        for source in self.reference_sources.get(row["id"], []):
            if source not in sources:
                sources.append(source)
        return {**row, "sources": sources}

    def _result(self, payload: dict) -> dict:
        if payload.get("success") is True:
            self.source_status = payload.get("source_status", self.source_status)
            return payload
        if "credential_register_unreadable" not in self.warnings:
            self.warnings.append("credential_register_unreadable")
        self.available = False
        return {"credentials": [], "total": 0, "overall_total": 0, "summary": {}, "datasets": []}

    def page(
        self, *, query: str | None = None, validation_status: str | None = None,
        limit: int = 25, offset: int = 0,
    ) -> dict[str, Any]:
        base = self._result(self.store.list_credentials(
            query=query, validation_status=validation_status, limit=limit, offset=offset,
            exclude_ids=self.exclude_ids,
        )) if self.available else self._result({})
        extra = [row for row in self.overlay if matches(row, query, validation_status)]
        rows = [self._attributed(row) for row in base["credentials"]]
        start = max(0, offset - base["total"])
        rows = [*rows, *extra[start:start + max(0, limit - len(rows))]]
        total = base["total"] + len(extra)
        return {
            "credentials": rows, "total": total,
            "overall_total": base["overall_total"] + len(self.overlay),
            "summary": summary(self.overlay, base["summary"]),
            "limit": limit, "offset": offset, "has_more": offset + len(rows) < total,
        }

    def report_snapshot(self) -> dict:
        base = self._result(self.store.report_snapshot(
            limit=REPORT_CREDENTIAL_LIMIT, exclude_ids=self.exclude_ids,
        )) if self.available else self._result({})
        candidates = [*base["credentials"], *self.overlay]
        candidates.sort(key=report_priority)
        rows = [self._attributed(row) for row in candidates[:REPORT_CREDENTIAL_LIMIT]]
        total = base["total"] + len(self.overlay)
        return {
            "credentials": rows,
            "credential_summary": {
                "total": total, "sample_count": len(rows), "sample_limit": REPORT_CREDENTIAL_LIMIT,
                "sampled": total > len(rows), "counts": summary(self.overlay, base["summary"]),
                "selection": "validated_first_then_severity",
                "full_inventory": "credentials.csv (complete inventory download in Credentials tab)",
                "datasets": (self.datasets or base.get("datasets", []))[:100],
                "dataset_count": base.get("dataset_count", 0),
            },
        }

    def iter_credentials(self) -> Iterator[dict]:
        if self.available:
            for row in self.store.iter_credentials(exclude_ids=self.exclude_ids):
                yield self._attributed(row)
        yield from self.overlay
