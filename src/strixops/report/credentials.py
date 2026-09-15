"""Deterministic credential inventory from recorded scan findings and notebook data.

No files, tools or model calls are used here. Recorded credentials are not proof
of successful authentication; only an explicit validation field changes status.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

_ALIASES = {
    "host": "host",
    "hostname": "host",
    "target": "host",
    "主机": "host",
    "主機": "host",
    "目标": "host",
    "目標": "host",
    "username": "username",
    "user": "username",
    "account": "username",
    "login": "username",
    "用户": "username",
    "使用者": "username",
    "用户名": "username",
    "用戶名": "username",
    "账号": "username",
    "帳號": "username",
    "password": "password",
    "passwd": "password",
    "pwd": "password",
    "pass": "password",
    "密码": "password",
    "密碼": "password",
    "hash": "hash",
    "passwordhash": "hash",
    "ntlm": "hash",
    "nthash": "hash",
    "lmhash": "hash",
    "哈希": "hash",
    "雜湊": "hash",
    "apikey": "api_key",
    "apitoken": "token",
    "token": "token",
    "accesstoken": "token",
    "refreshtoken": "token",
    "bearertoken": "token",
    "sessiontoken": "token",
    "secret": "secret",
    "clientsecret": "secret",
    "secretkey": "secret",
    "privatekey": "private_key",
    "encryptionkey": "encryption_key",
    "密钥": "secret",
    "金鑰": "secret",
    "source": "source",
    "来源": "source",
    "來源": "source",
    "severity": "severity",
    "note": "note",
    "notes": "note",
    "备注": "note",
    "備註": "note",
    "validationstatus": "validation_status",
    "validation": "validation_status",
    "验证状态": "validation_status",
    "驗證狀態": "validation_status",
    "secrettype": "secret_type",
    "credentialtype": "secret_type",
}
_SECRET_KEYS = ("password", "hash", "api_key", "token", "secret", "private_key", "encryption_key")
_TEXT_FIELDS = {
    "content",
    "description",
    "evidence",
    "technical_analysis",
    "impact",
    "note",
    "notes",
    "executive_summary",
    "battle_gains",
    "attack_narrative",
    "environment_map",
    "credential_capabilities",
    "future_leverage",
    "business_impact",
    "limitations",
    "methodology",
    "recommendations",
    "counterevidence",
    "rationale",
    "addendum",
}
_SKIP_FIELDS = {
    "poc_script_code",
    "poc_description",
    "code",
    "script",
    "history",
    "update_history",
    "scan_config",
}
_PLACEHOLDERS = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "redacted",
    "masked",
    "your_password",
    "your_api_key",
    "<password>",
    "<token>",
    "<secret>",
    "***",
    "****",
    "*****",
    "未发现",
    "未知",
    "已脱敏",
    "无",
    "無",
}
_SPECULATIVE = re.compile(
    r"\b(?:example|placeholder|sample|pseudocode|hypothetical|try|candidate|wordlist)\b|"
    r"(?:示例|範例|例如|占位|假设|假設|尝试|嘗試|候选|候選)",
    re.I,
)
_LABEL = re.compile(
    r"(?<![\w])(?:[\"'`*]{0,2})(username|user|account|login|password|passwd|pwd|pass|"
    r"password[_ -]?hash|hash|ntlm|nt[_ -]?hash|lm[_ -]?hash|api[_ -]?key|api[_ -]?token|"
    r"access[_ -]?token|refresh[_ -]?token|bearer[_ -]?token|session[_ -]?token|token|"
    r"client[_ -]?secret|secret[_ -]?key|private[_ -]?key|encryption[_ -]?key|secret|"
    r"hostname|host|target|validation[_ -]?status|用户名|用戶名|用户|使用者|账号|帳號|密码|密碼|"
    r"哈希|雜湊|密钥|金鑰|主机|主機|目标|目標|验证状态|驗證狀態)"
    r"(?:[\"'`*]{0,2})\s*[:=：]\s*(?:\*\*)?",
    re.I,
)


def _key(value: str) -> str:
    return _ALIASES.get(re.sub(r"[\s_\-*`]+", "", value).casefold(), "")


def _scalar(value: Any) -> str:
    # Do not reinterpret numbers/bools as passwords: their original bytes are lost.
    if not isinstance(value, str) or "\x00" in value:
        return ""
    try:
        value.encode("utf-8")
    except UnicodeError:
        return ""
    return value


def _literal(value: str) -> str:
    value = value.strip()
    if value.startswith("`"):
        width = len(value) - len(value.lstrip("`"))
        if len(value) >= 2 * width and value.endswith("`" * width):
            return value[width:-width]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "`\"'":
        return value[1:-1]
    return value


def _label_matches(text: str) -> list[re.Match[str]]:
    """Only treat labels outside a field's quoted literal as another field.

    Quotes in the middle of an unquoted value are ordinary secret bytes. A
    quoted value starts immediately after its label; escaped quotes and inline
    backtick delimiters cannot introduce a different username or host.
    """
    matches: list[re.Match[str]] = []
    protected_until = 0
    for match in _LABEL.finditer(text):
        if match.start() < protected_until:
            continue
        matches.append(match)
        start = match.end()
        if start >= len(text) or text[start] not in "\"'`":
            continue
        delimiter = text[start]
        if delimiter == "`":
            width = len(text[start:]) - len(text[start:].lstrip("`"))
            delimiter *= width
        position = start + len(delimiter)
        protected_until = len(text)
        while position < len(text):
            closing = text.find(delimiter, position)
            if closing < 0:
                break
            preceding = text[:closing]
            escapes = len(preceding) - len(preceding.rstrip("\\"))
            if delimiter[0] != "`" and escapes % 2:
                position = closing + len(delimiter)
                continue
            protected_until = closing + len(delimiter)
            break
    return matches


def _usable(value: str) -> bool:
    stripped = value.strip()
    return (
        stripped.casefold() not in _PLACEHOLDERS
        and not re.fullmatch(r"[*•●]+", stripped)
        and "\x00" not in value
    )


def _status(value: Any) -> str:
    normalized = _scalar(value).strip().casefold().replace("-", "_")
    if normalized in {"validated", "verified", "confirmed", "success", "successful", "已验证", "已驗證"}:
        return "validated"
    if normalized in {"failed", "invalid", "rejected", "验证失败", "驗證失敗"}:
        return "failed"
    if normalized in {"", "unverified", "not_tested", "untested", "未验证", "未驗證"}:
        return "unverified"
    return "unknown"


def _normalized(row: dict, defaults: dict) -> Iterable[dict]:
    values = {
        _key(k): _scalar(v)
        for k, v in row.items()
        if isinstance(k, str) and _key(k) and isinstance(v, str) and (v == "" or _scalar(v))
    }
    base = {
        key: values.get(key) or defaults.get(key, "")
        for key in ("host", "username", "source", "severity", "note")
    }
    base["validation_status"] = _status(values.get("validation_status"))
    secrets = [(key, values[key]) for key in _SECRET_KEYS if key in values and _usable(values[key])]
    if (
        not secrets
        and values.get("password") == ""
        and (
            base["username"]
            or values.get("secret_type") == "password"
            or base["validation_status"] == "validated"
        )
    ):
        secrets.append(("password", ""))
    if not secrets and _usable(base["username"]):
        yield {**base, "password": "", "hash": "", "secret_type": "username"}
    for key, value in secrets:
        yield {
            **base,
            "password": "" if key == "hash" else value,
            "hash": value if key == "hash" else "",
            "secret_type": values.get("secret_type") or key,
        }


def _markdown_cells(line: str) -> list[str]:
    # Escaped pipes inside secrets remain literal pipes, not column separators.
    return [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _text_rows(text: str) -> Iterable[dict]:
    if text.lstrip().startswith(("{", "[")):
        try:
            document = json.loads(text)
        except (ValueError, RecursionError):
            pass
        else:
            yield from _structured_rows(document)
            return
    lines = text.splitlines()
    table: list[str] | None = None
    fence = ""
    fenced: list[str] = []
    unfenced: list[str] = []
    labels: dict[str, str] = {}
    skipped_section = 0
    skipped_paragraph = False
    for raw in [*lines, ""]:
        stripped = raw.strip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading and not fence:
            if skipped_section and len(heading[1]) <= skipped_section:
                skipped_section = 0
            if _SPECULATIVE.search(heading[2]) or re.search(
                r"proof.of.concept|\bpoc\b|示例代码", heading[2], re.I
            ):
                skipped_section = len(heading[1])
        if skipped_section:
            continue
        if not stripped:
            skipped_paragraph = False
        if skipped_paragraph:
            continue
        marker = re.match(r"^(`{3,}|~{3,})(.*)$", stripped)
        if marker:
            if fence:
                if marker[1][0] == fence[0]:
                    if fence.endswith(":json"):
                        try:
                            value = json.loads("\n".join(fenced))
                            yield from _structured_rows(value)
                        except (ValueError, RecursionError):
                            pass
                    elif fence.endswith(":csv"):
                        with suppress(csv.Error):
                            yield from _csv_rows("\n".join(fenced))
                    elif fence.rsplit(":", 1)[-1] in {"text", "plaintext", "ini", "env", "log", "output"}:
                        yield from _text_rows("\n".join(fenced))
                    fence, fenced = "", []
            else:
                fence = marker[1] + ":" + marker[2].strip().casefold()
            if labels:
                yield labels
                labels = {}
            continue
        if fence:
            fenced.append(raw)
            continue
        unfenced.append(raw)
        if "|" in stripped:
            cells = _markdown_cells(stripped)
            if table and len(cells) == len(table):
                if not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                    yield {key: _literal(cell) for key, cell in zip(table, cells, strict=True) if key}
                continue
            keys = [_key(cell) for cell in cells]
            if any(key in _SECRET_KEYS for key in keys) or "username" in keys:
                table = keys
                if labels:
                    yield labels
                    labels = {}
                continue
        table = None
        # A CSV header is handled separately, without parsing unrelated prose as CSV.
        matches = _label_matches(stripped)
        prose = stripped[: matches[0].start()] if matches else stripped
        if _SPECULATIVE.search(prose):
            skipped_paragraph = not matches
            if labels:
                yield labels
                labels = {}
            continue
        if not matches:
            if labels:
                yield labels
                labels = {}
            continue
        current: dict[str, str] = {}
        for index, match in enumerate(matches):
            ending = matches[index + 1].start() if index + 1 < len(matches) else len(stripped)
            value = stripped[match.end() : ending]
            # Separators belong to syntax only when another field follows.
            if index + 1 < len(matches):
                value = value.rstrip(" ,;")
            current[_key(match[1])] = _literal(value)
        prefix = stripped[: matches[0].start()].strip(" -*\t")
        if (prefix or set(current) & set(labels)) and labels:
            yield labels
            labels = {}
        if prefix:
            yield current
        else:
            labels.update(current)
    # Unfenced CSV blocks are common in saved credential findings.
    for start, raw in enumerate(unfenced):
        try:
            header = next(csv.reader([raw]))
        except (csv.Error, StopIteration):
            continue
        keys = [_key(cell) for cell in header]
        if len(keys) >= 2 and any(k in _SECRET_KEYS for k in keys):
            block = []
            for line in unfenced[start:]:
                if not line.strip():
                    break
                block.append(line)
            with suppress(csv.Error):
                yield from _csv_rows("\n".join(block))
            break


def _csv_rows(text: str) -> Iterable[dict]:
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    keys = [_key(cell.lstrip("\ufeff")) for cell in header]
    if not any(key in _SECRET_KEYS for key in keys) and "username" not in keys:
        return
    for row in reader:
        if len(row) == len(keys):
            yield {key: value for key, value in zip(keys, row, strict=True) if key}


def _structured_rows(value: Any, depth: int = 0, context: dict | None = None) -> Iterable[dict]:
    if depth > 12:
        return
    context = dict(context or {})
    if isinstance(value, list):
        for item in value:
            yield from _structured_rows(item, depth + 1, context)
    elif isinstance(value, dict):
        context.update(
            {
                _key(key): _scalar(item)
                for key, item in value.items()
                if isinstance(key, str)
                and _key(key) in {"host", "username", "source", "severity"}
                and isinstance(item, str)
            }
        )
        keys = {_key(key) for key in value if isinstance(key, str)}
        if keys.intersection(_SECRET_KEYS) or "username" in keys:
            yield {**context, **value}
        for key, nested in value.items():
            if key in _SKIP_FIELDS:
                continue
            if isinstance(nested, (dict, list)):
                yield from _structured_rows(nested, depth + 1, context)
            elif isinstance(nested, str) and key in _TEXT_FIELDS | {"credentials", "secrets"}:
                for row in _text_rows(nested):
                    yield {**context, **row}


def collect_credentials(
    *,
    reports: list[dict],
    internal_findings: list[dict],
    notes: list[dict] | dict,
    assessment: dict,
    final_fields: dict | None,
    campaign: dict | None = None,
) -> list[dict[str, Any]]:
    """Collect explicit recorded values, retaining source identity and exact secrets."""
    records: list[tuple[str, dict]] = []
    records.extend(("vulnerability", row) for row in reports if isinstance(row, dict))
    records.extend(("finding", row) for row in internal_findings if isinstance(row, dict))
    note_rows = notes.get("notes", []) if isinstance(notes, dict) else notes
    records.extend(("note", row) for row in note_rows if isinstance(row, dict) and not row.get("deleted"))
    for key, child, kind in (
        ("coverage", "entries", "coverage"),
        ("threat_models", "models", "threat_model"),
    ):
        section = assessment.get(key, {}) if isinstance(assessment, dict) else {}
        if isinstance(section, dict):
            records.extend((kind, row) for row in section.get(child, []) if isinstance(row, dict))
    if isinstance(final_fields, dict):
        records.append(("root", final_fields))
    if isinstance(campaign, dict):
        records.append(("campaign", campaign))
    extracted: list[dict] = []
    for index, (kind, record) in enumerate(records):
        source_id = (
            _scalar(record.get("id") or record.get("note_id") or record.get("target"))
            or f"{kind}-{index + 1}"
        )
        title = _scalar(record.get("title") or record.get("risk_area") or record.get("target")) or source_id
        provenance = {"kind": kind, "id": source_id, "title": title}
        defaults = {key: _scalar(record.get(key)) for key in ("host", "username", "source", "severity")}
        defaults["host"] = defaults["host"] or _scalar(record.get("target"))
        defaults["source"] = defaults["source"] or source_id
        for candidate in _structured_rows(record):
            for row in _normalized(candidate, defaults):
                extracted.append({**row, "sources": [provenance]})
    return merge_credentials(extracted)


def merge_credentials(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge recorded and imported inventories without losing value/source mappings."""
    collected: dict[tuple[str, ...], dict] = {}
    notes_seen: dict[tuple[str, ...], set[str]] = {}
    severity_order = {"": -1, "info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    for group in groups:
        for row in group:
            identity = tuple(
                row.get(key, "") for key in ("host", "username", "password", "hash", "secret_type")
            )
            if identity not in collected:
                digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()[
                    :20
                ]
                collected[identity] = {**row, "id": f"cred-{digest}", "sources": list(row.get("sources", []))}
                notes_seen[identity] = {row.get("note", "")}
                continue
            previous = collected[identity]
            for provenance in row.get("sources", []):
                if provenance not in previous["sources"]:
                    previous["sources"].append(provenance)
            value = row.get("source", "")
            if value and value not in previous["source"].split("\n"):
                previous["source"] = "\n".join(filter(None, [previous["source"], value]))
            note = row.get("note", "")
            if note and note not in notes_seen[identity]:
                previous["note"] = "\n".join(filter(None, [previous["note"], note]))
                notes_seen[identity].add(note)
            if severity_order.get(row.get("severity", "").casefold(), -1) > severity_order.get(
                previous["severity"].casefold(), -1
            ):
                previous["severity"] = row["severity"]
            statuses = {previous["validation_status"], row["validation_status"]} - {"unverified"}
            previous["validation_status"] = (
                next(iter(statuses)) if len(statuses) == 1 else "unknown" if statuses else "unverified"
            )
    return list(collected.values())


def credential_source_warnings(
    *,
    reports: list[dict],
    internal_findings: list[dict],
    credentials: list[dict],
) -> list[str]:
    """A recorded credential finding without extractable values is not an all-clear."""
    covered = {
        source.get("id")
        for row in credentials
        for source in row.get("sources", [])
        if isinstance(source, dict)
    }
    known_ids = {row.get("id") for row in credentials}
    for finding in [*reports, *internal_findings]:
        references = _credential_references(finding) if isinstance(finding, dict) else set()
        if (
            isinstance(finding, dict)
            and finding.get("finding_type") == "credential"
            and (
                bool(references - known_ids)
                or (finding.get("id") not in covered and not references)
            )
            and (finding.get("content") or finding.get("metadata"))
        ):
            return ["credential_records_unparsed"]
    return []


def _credential_references(record: dict[str, Any]) -> set[str]:
    """Read current explicit register references without old histories or scripts."""
    references: set[str] = set()
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        values = metadata.get("credential_ids", [])
        if isinstance(values, list):
            references.update(value for value in values if isinstance(value, str))
        value = metadata.get("credential_id")
        if isinstance(value, str):
            references.add(value)
    for field in ("content", "description", "evidence", "technical_analysis"):
        value = record.get(field)
        if isinstance(value, str):
            # Saved internal Markdown contains its structured Metadata again.
            # Read only current explicit metadata keys above, not history or
            # examples serialized inside that trailing JSON block.
            value = re.sub(r"\n## Metadata\s*\n```json\s*\n.*\n```\s*$", "", value, flags=re.S)
            references.update(re.findall(r"\bcred-[0-9a-f]{20}\b", value))
    return references


def merge_credential_inventory(
    registered: list[dict[str, Any]], *supplemental: list[dict[str, Any]],
    reports: Iterable[dict[str, Any]] = (), internal_findings: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Keep registered current validation authoritative while preserving other sources.

    Discovery parsing is a fallback. An older finding or CSV must not undo an
    explicit later registry update, or turn it into an unknown status. Conflicting
    supplemental observations remain attributable separately from current state.
    """
    def identity(row: dict[str, Any]) -> tuple[str, ...]:
        return tuple(row.get(key, "") for key in ("host", "username", "password", "hash", "secret_type"))

    current = {identity(row): row for row in registered}
    observations: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for group in supplemental:
        for row in group:
            key = identity(row)
            if key not in current:
                continue
            status = row.get("validation_status", "unverified")
            if status in {"unverified", current[key]["validation_status"]}:
                continue
            observed = {"validation_status": status, "sources": row.get("sources", [])}
            if observed not in observations.setdefault(key, []):
                observations[key].append(observed)
    result = merge_credentials(registered, *supplemental)
    for row in result:
        key = identity(row)
        if key not in current:
            continue
        record = current[key]
        for field in (
            "validation_status", "validation_evidence", "severity", "revision",
            "created_by", "updated_by", "created_at", "updated_at",
        ):
            if field in record:
                row[field] = record[field]
        if key in observations:
            row["supplemental_validation"] = observations[key]
    by_id = {row["id"]: row for row in result if identity(row) in current}
    for kind, records in (("vulnerability", reports), ("finding", internal_findings)):
        for record in records:
            if not isinstance(record, dict):
                continue
            source_id = _scalar(record.get("id"))
            provenance = {
                "kind": kind, "id": source_id,
                "title": _scalar(record.get("title")) or source_id,
            }
            for reference in _credential_references(record):
                row = by_id.get(reference)
                if row is not None and provenance not in row["sources"]:
                    row["sources"].append(provenance)
    return result
