"""LLM synthesis of the client-facing executive report.

Ports the reference platform's dedicated report worker: when a run finishes,
the full findings corpus (vulnerability reports, internal findings, campaign
ledger, coverage records) plus the root agent's finish_scan draft narrative
are sent to a fresh model call under the platform's editorial rules, and the
model composes the deliverable markdown in the platform report format.

The deterministic composer in :mod:`strixops.report.state` stays as the
fallback for dry runs, model failures and timeouts — a run never ends without
a report file. The synthesis call mirrors ``report.dedupe``: one
``model.get_response`` through the run's tracked model route, with a
per-attempt timeout and a findings-only retry when the full corpus fails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agents import ModelSettings, ModelTracing

from strixops.engine.resilience import MODEL_RETRY
from strixops.engine.targets import normalize_targets
from strixops.report.dedupe import _extract_text
from strixops.report.state import RunState

if TYPE_CHECKING:
    from agents import Model

logger = logging.getLogger(__name__)

# Per-string cap for corpus fields — keeps one runaway evidence blob from
# eating the whole context window. The old worker capped nothing because its
# source files were pre-curated; here the reports are model-authored.
_FIELD_CHAR_LIMIT = 6000
# Total corpus budget for the full attempt (characters, roughly 50k tokens).
_SOURCE_CHAR_BUDGET = 200_000

_DEFAULT_TIMEOUT = 900.0


def synthesis_enabled() -> bool:
    """Kill switch for the synthesis pass — ``STRIXOPS_REPORT_SYNTHESIS=0``
    forces the deterministic composer (fixture lifecycles, incidents)."""
    return (os.environ.get("STRIXOPS_REPORT_SYNTHESIS") or "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }

_SEVERITY_RULES_ZH = """- Use only canonical severity labels (Critical/High/Medium/Low/Info). Never use
  Elevated, Medium-High, moderate-high, 偏高, or any other non-canonical label.
- Overall severity scoring rules:
  - Critical: only if confirmed RCE exists.
  - High: no confirmed RCE, but clear high-impact compromise, high-value data
    access, privileged access, or broadly reusable secrets exist.
  - Medium: meaningful exploitable weakness or exposure exists, but leverage/impact
    is still limited.
  - Low: minor weakness or low-impact exposure only.
  - Info: observational or preparatory findings only."""

_ZH_SECTIONS = [
    "1. 執行摘要",
    "2. 本階段戰果整理",
    "3. 攻擊路徑與關鍵進展",
    "4. 內網架構、關鍵主機與服務",
    "5. 重要發現與技術細節",
    "6. 憑證、雜湊與存取能力",
    "7. 後續可利用路徑",
    "8. 敏感資料與業務衝擊",
    "9. 本階段限制與未完成部分",
]

_EN_SECTIONS = [
    "1. Executive Summary",
    "2. Engagement Gains",
    "3. Attack Path & Key Progress",
    "4. Architecture, Key Hosts & Services",
    "5. Findings & Technical Detail",
    "6. Credentials, Hashes & Access Capabilities",
    "7. Future Leverage Paths",
    "8. Sensitive Data & Business Impact",
    "9. Limitations & Unfinished Work",
]


def _cap(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _FIELD_CHAR_LIMIT:
        return value[:_FIELD_CHAR_LIMIT] + "\n…[truncated]"
    return value


def _cap_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: _cap(value) for key, value in payload.items()}


_REPORT_FIELDS = (
    "id",
    "title",
    "severity",
    "cvss",
    "cvss_vector",
    "description",
    "impact",
    "target",
    "endpoint",
    "method",
    "cve",
    "cwe",
    "technical_analysis",
    "poc_description",
    "poc_script_code",
    "evidence",
    "counterevidence",
    "confidence",
    "confidence_rationale",
    "severity_change_conditions",
    "fix_effort",
    "finding_class",
)

_FINDING_FIELDS = (
    "id",
    "finding_type",
    "title",
    "content",
    "host",
    "source",
    "severity",
    "metadata",
    "agent_name",
)


def _selected(payload: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return _cap_mapping(
        {key: value for key, value in payload.items() if key in fields and value not in ("", None, [], {})}
    )


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


_BR_SUFFIX = re.compile(r"\s*<br\s*/?\s*>\s*$", re.IGNORECASE)
_TARGET_HEADER = re.compile(r"^(?:[-*]\s+)?(?:targets?|目[标標])(?:\s*[（(][^）)]*[）)])?\s*[:：]", re.I)
_HEADER_LIST_ITEM = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")


def normalize_report_header(
    content: str, *, targets: list[str] | None = None, language: str = "zh-CN"
) -> str:
    """Guarantee one header field per paragraph in the platform-format header.

    The console renders the report with react-markdown (no ``breaks``
    option), so consecutive single-newline lines soft-wrap into one run-on
    paragraph. The reference platform solved this with ``<br>`` suffixes;
    this renderer does not render raw HTML at all, so a model imitating the
    old format loses the breaks entirely. Rebuild the block before the first
    ``---`` with a blank line between every field — mirroring the
    deterministic composer — and leave the report body untouched.
    Multi-target reports replace the model's scope field with the complete
    trusted list, including targets absent from its findings or narrative.
    """
    lines = content.splitlines()
    index = next((i for i, line in enumerate(lines) if i > 0 and line.strip() == "---"), None)
    multi = targets is not None and len(targets) > 1
    if index is None:
        if not multi:
            return content
        # A usable model report may omit the separator. Keep its body intact
        # and add the trusted scope to the title/header before the first section.
        index = next((i for i, line in enumerate(lines) if i > 0 and line.startswith("#")), 1)
    header = [_BR_SUFFIX.sub("", item) for item in lines[:index] if item.strip()]
    if not header:
        return content
    if multi:
        retained = []
        target_list = False
        for item in header:
            if _TARGET_HEADER.match(item.replace("**", "").strip()):
                target_list = True
                continue
            if target_list and _HEADER_LIST_ITEM.match(item.strip()):
                continue
            target_list = False
            retained.append(item)
        label = "目標" if language.startswith("zh") else "Targets"
        scope = f"{label} ({len(targets)}):\n\n" + "\n".join(f"- {target}" for target in targets)
        retained.insert(1 if retained and retained[0].startswith("#") else 0, scope)
        header = retained
    rebuilt: list[str] = []
    for item in header:
        if rebuilt:
            rebuilt.append("")
        rebuilt.append(item)
    if multi:
        rebuilt.append("")
    rebuilt.extend(lines[index:])
    return "\n".join(rebuilt) + "\n"


def synthesis_system_prompt(*, language: str) -> str:
    """Editorial contract for the report composer (ported from the platform worker)."""
    zh = (language or "zh-CN").startswith("zh")
    if zh:
        deliverable = (
            "Traditional Chinese (繁體中文). Tool identifiers, code, commands, "
            "protocol strings and finding ids stay as-is."
        )
        sections = "\n".join(f"  {item}" for item in _ZH_SECTIONS)
    else:
        deliverable = "English."
        sections = "\n".join(f"  {item}" for item in _EN_SECTIONS)
    return f"""You are composing the final client-facing penetration-test report for one
completed run. You are a report editor, not a scanner: everything you write is
grounded in the REPORT SOURCE provided in the user message. Return ONLY the
final markdown report — no commentary before or after.

ABSOLUTE FIDELITY RULES:
- Every technical claim must come from the vulnerability reports, findings,
  campaign ledger, coverage records, or the root agent's draft narrative.
- The root draft fields are the operator's lead material; when they disagree
  with the filed reports, the filed reports win. Never invent endpoints,
  payloads, credentials, responses or hosts that are not in the source.
- Do not omit findings that are in the source. Cluster related findings into
  themes and explain each theme in depth; cite finding ids (e.g. vuln-0001)
  inline where the detail comes from.

OUTPUT FORMAT — start the report with exactly this header block. Keep the
blank line between every field: the report viewer renders markdown, and
without a blank line the fields collapse into one run-on paragraph. Never use
HTML tags such as <br> — the viewer does not render raw HTML.
# 滲透測試報告 - <target>            (English runs: # Penetration Test Report - <target>)

目標：<target>                       (English: Target: <target>)

任務類型：<task type>                 (English: Engagement type: <task type>)

報告產生時間：<generated_at, copy verbatim>

Overall Severity: <Critical|High|Medium|Low|Info>

Severity Rationale: <1-3 concise sentences>

---

Then include these sections (in order, with ## headings, when evidence exists):
{sections}

EDITORIAL RULES:
- Make the report detailed and concrete, not terse — and structured, never a
  wall of text. Length should be proportional to the evidence, never padded.
- In the findings section, for each theme: root cause, technical mechanism,
  concrete evidence (response excerpts, values, error messages), PoC summary
  (runnable code blocks allowed from poc_script_code), impact and severity
  justification.
- In the credentials section, list EVERY credential, hash, key and secret in
  the source completely — one per line or in a table. Never summarize as
  "N accounts found".
- In the architecture section, organize by hosts, services and access paths,
  and state what each system appears to do. For web engagements cover the
  observed external infrastructure and application components the same way.
- In the future-leverage section, explain what can be exploited next from the
  access and secrets already obtained.
- In the limitations section, state coverage gaps honestly; if the run status
  says the execution ended early or failed, clearly state coverage is partial.
- Do NOT include remediation advice anywhere.
- Do NOT cite source file names unless necessary to explain the operation.

TYPOGRAPHY — the viewer renders full markdown (headings, lists, GFM tables,
fenced code blocks and mermaid diagrams), so use it:
- Keep paragraphs short: at most 5 lines each. Anything a section enumerates
  becomes bullets or a table, not run-on prose.
- Break major sections into ### subsections with meaningful titles, one per
  theme, stage or host (e.g. a numbered stage inside the attack-path section,
  one ### per host in the architecture section).
- Use GFM tables with a header row for anything tabular: host/service
  inventories, credentials, affected parameters, per-finding summaries.
- Bold the facts a reader must not miss: severity, endpoints, finding ids.
- PoC steps, commands and response excerpts go in fenced code blocks with
  their language tag.
- When a flow shows more than prose — an attack chain (entry → pivot →
  objective), an access path, or the environment layout — add ONE fenced
  mermaid flowchart (```mermaid, flowchart TD, ASCII node ids, labels in the
  report language, roughly 12 nodes at most). Diagrams support the written
  evidence; they never replace it, and a section gets at most one.
{_SEVERITY_RULES_ZH}

The report language is {deliverable}"""


def build_report_source(
    run_state: RunState,
    *,
    generated_at: str,
    trimmed: bool = False,
) -> str:
    """Assemble the synthesis corpus from durable run state.

    ``trimmed`` mirrors the old worker's findings-only fallback: coverage,
    the campaign ledger and finding bodies are dropped so a retry fits when
    the full corpus failed (timeout, overflow).
    """
    scan_config = run_state.run_record.get("scan_config") or {}
    status = str(run_state.run_record.get("status") or "unknown")
    header = {
        "target": scan_config.get("target") or run_state.run_dir.name,
        "scan_type": scan_config.get("scan_type") or "web",
        "crypto_mode": scan_config.get("crypto_mode", False),
        "report_language": scan_config.get("report_language") or "zh-CN",
        "run_status": status,
        "duration_seconds": run_state.duration_seconds(),
        "generated_at": generated_at,
        "vulnerability_report_count": len(run_state.reports),
        "internal_finding_count": len(run_state.internal_findings),
    }
    if "targets" in scan_config:
        targets = normalize_targets(scan_config.get("target", ""), scan_config["targets"])
        if len(targets) > 1:
            header.update(targets=targets, target_count=len(targets))
    if scan_config.get("socks5_proxy"):
        header["declared_access"] = f"socks5: {scan_config['socks5_proxy']}"
    elif scan_config.get("gsocket_key"):
        header["declared_access"] = "gsocket: <key supplied>"

    parts = ["# REPORT SOURCE", "", "## Run Overview", _dump_json(header), ""]

    final_fields = run_state.final_fields
    if final_fields:
        parts += [
            "## Root Agent Draft Narrative (finish_scan fields — leads, verify against the filed reports)",
            _dump_json(_cap_mapping(final_fields)),
            "",
        ]

    if run_state.reports:
        parts += ["## Vulnerability Reports (full)", ""]
        for report in run_state.reports:
            rid = report.get("id") or "?"
            title = report.get("title") or ""
            severity = str(report.get("severity") or "").upper()
            parts += [f"### {rid} — {title} [{severity}]", _dump_json(_selected(report, _REPORT_FIELDS)), ""]

    if run_state.internal_findings:
        parts += ["## Internal Findings (full)", ""]
        for finding in run_state.internal_findings:
            fid = finding.get("id") or "?"
            title = finding.get("title") or ""
            if trimmed:
                parts.append(f"- {fid} — {title} (body omitted in retry source)")
            else:
                parts += [f"### {fid} — {title}", _dump_json(_selected(finding, _FINDING_FIELDS)), ""]

    campaign = run_state.run_record.get("internal_campaign")
    if campaign and not trimmed:
        parts += [
            "## Internal Campaign Ledger (engagement-created resources and observations)",
            _dump_json(_cap_mapping(campaign)),
            "",
        ]

    try:
        coverage = run_state.assessment.snapshot(status)["coverage"]
    except Exception:  # noqa: BLE001 — coverage is optional input, never fatal
        coverage = None
    if coverage and not trimmed:
        parts += [
            "## Coverage Records (agent-reported per-surface outcomes)",
            _dump_json(_cap_mapping(coverage)),
            "",
        ]

    source = "\n".join(parts).rstrip() + "\n"
    if len(source) > _SOURCE_CHAR_BUDGET and not trimmed:
        logger.warning(
            "report source %d chars exceeds budget %d; retry source will be trimmed",
            len(source),
            _SOURCE_CHAR_BUDGET,
        )
    return source


def _attempt_timeout() -> float:
    raw = os.environ.get("STRIXOPS_REPORT_SYNTHESIS_TIMEOUT") or ""
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


async def synthesize_executive_report(
    run_state: RunState,
    resolve_model: Callable[[], Model],
) -> str | None:
    """Compose the final report via one model call over the full corpus.

    Returns the report markdown, or ``None`` when synthesis is unavailable —
    the caller then falls back to the deterministic composer. Two attempts:
    full source, then the trimmed findings-only source (the old worker's
    fallback ladder, compressed).
    """
    language = run_state.report_language()
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    system = synthesis_system_prompt(language=language)
    model = resolve_model()
    timeout = _attempt_timeout()

    full_source = build_report_source(run_state, generated_at=generated_at)
    for attempt, trimmed in enumerate((False, True), start=1):
        if trimmed:
            source = build_report_source(run_state, generated_at=generated_at, trimmed=True)
            if len(source) >= len(full_source):
                # Nothing was actually trimmed; the retry cannot succeed where
                # the full attempt failed. Skip the second timeout.
                break
        else:
            source = full_source
        try:
            response = await asyncio.wait_for(
                model.get_response(
                    system_instructions=system,
                    input="Compose the final penetration-test report from the following REPORT SOURCE.\n\n"
                    + source,
                    model_settings=ModelSettings(
                        retry=MODEL_RETRY,
                        include_usage=True,
                        extra_args={"timeout": timeout} if timeout > 0 else None,
                    ),
                    tools=[],
                    output_schema=None,
                    handoffs=[],
                    tracing=ModelTracing.DISABLED,
                    previous_response_id=None,
                    conversation_id=None,
                    prompt=None,
                ),
                timeout=timeout + 60,
            )
        except TimeoutError:
            logger.warning("report synthesis attempt %d timed out after %.0fs", attempt, timeout)
            continue
        except Exception:
            logger.exception("report synthesis attempt %d failed", attempt)
            continue

        content = _extract_text(response).strip()
        if content.startswith("#") and len(content) > 500:
            scan_config = run_state.run_record.get("scan_config") or {}
            targets = (
                normalize_targets(scan_config.get("target", ""), scan_config["targets"])
                if "targets" in scan_config else None
            )
            report = normalize_report_header(content, targets=targets, language=language)
            logger.info("report synthesized on attempt %d (%d chars)", attempt, len(report))
            return report
        logger.warning(
            "report synthesis attempt %d produced unusable output (%d chars)", attempt, len(content)
        )

    return None
