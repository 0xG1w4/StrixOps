"""Read-only, request-scoped credential inventory across a project's tasks."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from strixops.console import credentials
from strixops.report.credential_store import _IDENTITY, _LIMITS, VALIDATION_STATUSES
from strixops.report.credentials import merge_credentials

_CATEGORY_TYPES = {
    "password": ("password",),
    "key": ("api_key", "token", "secret", "private_key", "encryption_key"),
    "hash": ("hash",),
}


def _attributed(row: dict, run: str) -> dict:
    """Retain each task's evidence, including contradictory current validations."""
    sources = [
        {**source, "title": f"{run} · {source.get('title') or source.get('kind') or 'Credential'}"}
        for source in row.get("sources", [])
    ]
    if not sources:
        sources = [{"kind": "credential_register", "id": row["id"], "title": run}]
    evidence = f"[{run}] validation={row.get('validation_status', 'unverified')}"
    if row.get("validation_evidence"):
        evidence += "\n" + row["validation_evidence"]
    supplemental = []
    for observed in row.get("supplemental_validation", []):
        observed_sources = [
            {**source, "title": f"{run} · {source.get('title') or source.get('kind') or 'Credential'}"}
            for source in observed.get("sources", [])
        ]
        supplemental.append({**observed, "sources": observed_sources})
        evidence += f"\n[{run}] supplemental_validation={observed.get('validation_status', 'unknown')}"
    return {
        **{field: row.get(field, "") for field in (*_IDENTITY, "severity")},
        "id": row["id"],
        "source": f"[{run}] {row.get('source') or 'Credential inventory'}",
        "note": f"[{run}] {row['note']}" if row.get("note") else "",
        "validation_status": row.get("validation_status", "unverified"),
        "validation_evidence": evidence,
        "sources": sources,
        "source_runs": [run],
        "source_count": 1,
        **({"supplemental_validation": supplemental} if supplemental else {}),
    }


class ProjectCredentialInventory:
    """Stream full task inventories into an automatically deleted SQLite database.

    The temporary database bounds the aggregate's resident memory and is closed
    after each JSON request or CSV stream. No project credential copy or cache is
    persisted, and pagination/search are applied only after exact deduplication.
    """

    def __init__(
        self, run_dirs: Iterable[Path], open_file: credentials.OpenRunFile, *,
        valid_assessment: Callable[[Any], bool] | None = None,
    ) -> None:
        # An empty SQLite filename creates a private temporary database that is
        # deleted when closed. CSV iteration may move between worker threads.
        self.connection = sqlite3.connect("", check_same_thread=False)
        self.closed = False
        self.warnings: list[str] = []
        self.contributing_run_count = 0
        self.run_count = 0
        self.source_status = "missing"
        try:
            self.connection.executescript(
                "PRAGMA cache_size=-2048; PRAGMA temp_store=FILE; PRAGMA journal_mode=OFF;"
                "CREATE TABLE inventory (identity TEXT PRIMARY KEY, host TEXT, username TEXT, "
                "validation_status TEXT, secret_type TEXT, severity TEXT, search_text TEXT, record TEXT);"
            )
            self._load(run_dirs, open_file, valid_assessment=valid_assessment)
            self.connection.commit()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> ProjectCredentialInventory:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.connection.close()

    def _warn(self, warning: str) -> None:
        if warning not in self.warnings:
            self.warnings.append(warning)

    def _add(self, row: dict, run: str) -> None:
        identity = json.dumps(tuple(row.get(key, "") for key in _IDENTITY), ensure_ascii=False)
        found = self.connection.execute(
            "SELECT record FROM inventory WHERE identity=?", (identity,),
        ).fetchone()
        observed = _attributed(row, run)
        if found:
            previous = json.loads(found[0])
            merged = merge_credentials([previous], [observed])[0]
            merged["source_runs"] = sorted(set(previous["source_runs"]) | {run})
            merged["source_count"] = len(merged["source_runs"])
            merged["validation_evidence"] = "\n".join(
                dict.fromkeys([previous["validation_evidence"], observed["validation_evidence"]])
            )
            supplemental = list(previous.get("supplemental_validation", []))
            for value in observed.get("supplemental_validation", []):
                if value not in supplemental:
                    supplemental.append(value)
            if supplemental:
                merged["supplemental_validation"] = supplemental
        else:
            merged = observed
        search_text = "\n".join([
            *(str(merged.get(key, "")) for key in _LIMITS),
            *(source.get("title", "") for source in merged["sources"]),
            *merged["source_runs"],
        ]).casefold()
        self.connection.execute(
            "INSERT OR REPLACE INTO inventory VALUES (?,?,?,?,?,?,?,?)",
            (identity, merged["host"], merged["username"], merged["validation_status"],
             merged["secret_type"], merged["severity"], search_text,
             json.dumps(merged, ensure_ascii=False)),
        )

    def _load(self, run_dirs: Iterable[Path], open_file: credentials.OpenRunFile, *,
              valid_assessment: Callable[[Any], bool] | None) -> None:
        directories = sorted({Path(directory).absolute() for directory in run_dirs}, key=str)
        self.run_count = len(directories)
        available = False
        for run_dir in directories:
            contributed = False
            try:
                inventory, reader = credentials.load_inventory(
                    run_dir, open_file, valid_assessment=valid_assessment,
                )
            except (OSError, ValueError):
                self._warn("credential_sources_unreadable")
                continue
            rows = inventory.iter_credentials()
            try:
                for row in rows:
                    self._add(row, run_dir.name)
                    contributed = True
            except (OSError, ValueError):
                reader.warn("credential_register_unreadable")
                # A late register failure must not discard separately readable
                # findings and imported CSV observations for this task.
                for row in inventory.overlay:
                    self._add(row, run_dir.name)
                    contributed = True
            finally:
                rows.close()
            state = credentials.source_state(inventory, reader)
            available = available or state["source_status"] in {"available", "partial"} or contributed
            for warning in state["warnings"]:
                self._warn(warning)
            self.contributing_run_count += int(contributed)
        self.source_status = (
            ("partial" if available else "unreadable") if self.warnings
            else ("available" if available else "missing")
        )

    def metadata(self) -> dict:
        return {
            "run_count": self.run_count,
            "contributing_run_count": self.contributing_run_count,
            "source_status": self.source_status,
            "warnings": self.warnings,
        }

    def page(self, *, query: str | None = None, validation_status: str | None = None,
             secret_category: str | None = None, limit: int = 25, offset: int = 0) -> dict:
        clauses, values = [], []
        if query:
            clauses.append("instr(search_text, ?) > 0")
            values.append(query.casefold())
        if validation_status:
            clauses.append("validation_status=?")
            values.append(validation_status)
        if secret_category is not None:
            secret_types = _CATEGORY_TYPES.get(secret_category)
            if secret_types is None:
                raise ValueError("invalid secret category")
            clauses.append("secret_type IN (" + ",".join("?" for _ in secret_types) + ")")
            values.extend(secret_types)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        overall_total = self.connection.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        total = self.connection.execute("SELECT COUNT(*) FROM inventory" + where, values).fetchone()[0]
        rows = [json.loads(record) for (record,) in self.connection.execute(
            "SELECT record FROM inventory" + where + " ORDER BY host,username,identity LIMIT ? OFFSET ?",
            [*values, limit, offset],
        )]
        summary = {}
        for field in ("validation_status", "secret_type", "severity"):
            counts = dict.fromkeys(VALIDATION_STATUSES, 0) if field == "validation_status" else {}
            counts.update(self.connection.execute(f"SELECT {field},COUNT(*) FROM inventory GROUP BY {field}"))
            summary[field] = counts
        return {
            "credentials": rows, "total": total, "overall_total": overall_total,
            "summary": summary, "limit": limit, "offset": offset,
            "has_more": offset + len(rows) < total, **self.metadata(),
        }

    def iter_credentials(self) -> Iterator[dict]:
        for (record,) in self.connection.execute(
            "SELECT record FROM inventory ORDER BY host,username,identity"
        ):
            yield json.loads(record)


def list_response(run_dirs: Iterable[Path], open_file: credentials.OpenRunFile, *,
                  valid_assessment: Callable[[Any], bool] | None = None, **query: Any) -> dict:
    with ProjectCredentialInventory(run_dirs, open_file, valid_assessment=valid_assessment) as inventory:
        return inventory.page(**query)
