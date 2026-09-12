"""Shared presentation guidance and conservative, text-preserving paragraph breaks."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from strixops import skills

REPORT_FORMAT_SKILL = "reporting/report_format"
logger = logging.getLogger(__name__)


def report_format_guidance(run_dir: Path | None = None) -> str:
    """Use the run's frozen skill when present; formatting is never mandatory I/O."""
    if run_dir is not None:
        snapshot = Path(run_dir) / ".state" / "prompt_resources.json"
        try:
            data = json.loads(snapshot.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except Exception:  # Formatting must never block a run, including malformed optional snapshots.
            return ""
        else:
            # Old snapshots deliberately keep their old instructions. A live
            # edit must not silently replace an active run's formatting rules.
            records = data.get("skills", []) if isinstance(data, dict) else []
            if not isinstance(records, list):
                return ""
            for row in records:
                if isinstance(row, dict) and row.get("id") == REPORT_FORMAT_SKILL:
                    content = row.get("content")
                    return content if isinstance(content, str) else ""
            return ""
    try:
        return skills.load_skill_markdown(REPORT_FORMAT_SKILL) or ""
    except Exception:  # An unavailable optional skill leaves the existing report instructions intact.
        return ""


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_BLOCK = re.compile(r"^(?:[ \t]|#{1,6}(?:\s|$)|[-+*]\s|\d+[.)]\s|>|[|<])")
_INLINE_OR_LITERAL = re.compile(r"[`*_\[\]{}<>\\|~]|https?://|\S+[=:]\S+|^[\w.-]+:\s", re.MULTILINE)
_SENTENCE_END = re.compile(r"[。！？](?:[」』”’\"])*|[.!?](?:[\"')])*(?=\s|$)")
_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)\s*$")
_MIN_PARAGRAPH = 240
_LONG_PARAGRAPH = 600
_UNSAFE_BREAK = re.compile(r"[。！？.!?](?:[」』”’\"')])*\s*(?:\d+[.)]\s)|[ \t]{2,}")


def _split_plain_paragraph(text: str) -> str:
    # Leave inline Markdown, links and technical material to their original
    # author. Only insert blank lines into long, unadorned narrative paragraphs.
    if len(text) < _LONG_PARAGRAPH or _INLINE_OR_LITERAL.search(text) or _UNSAFE_BREAK.search(text):
        return text
    breaks: list[int] = []
    previous = 0
    for match in _SENTENCE_END.finditer(text):
        if text[match.start()] == "." and match.start() and text[match.start() - 1].isdigit():
            continue
        end = match.end()
        if end - previous >= _MIN_PARAGRAPH and len(text) - end >= 80:
            breaks.append(end)
            previous = end
    for position in reversed(breaks):
        text = text[:position] + "\n\n" + text[position:]
    return text


def format_report_markdown(text: str) -> str:
    """Insert paragraph breaks only; preserve all other text and Markdown blocks.

    No model calls, rewriting, shortening or required formatting validation.
    Existing paragraphs, code fences, tables and list blocks stay intact.
    """
    try:
        return _format_report_markdown(text)
    except Exception as exc:
        logger.warning("Report formatting skipped after %s; original text retained", type(exc).__name__)
        return text


def _format_report_markdown(text: str) -> str:
    output: list[str] = []
    paragraph: list[str] = []
    fence = ""
    structured = False

    def flush() -> None:
        if paragraph:
            output.append(_split_plain_paragraph("".join(paragraph)))
            paragraph.clear()

    for line in text.splitlines(keepends=True):
        if fence:
            output.append(line)
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                fence = ""
            continue
        if match := _FENCE.match(line):
            flush()
            fence = match.group(1)
            output.append(line)
            structured = False
        elif _SETEXT.fullmatch(line):
            # A setext heading includes the preceding paragraph. Splitting it
            # would silently turn most of the heading into body text.
            output.extend(paragraph)
            paragraph.clear()
            output.append(line)
            structured = True
        elif not line.strip():
            flush()
            output.append(line)
            structured = False
        elif _BLOCK.match(line) or "|" in line or structured:
            flush()
            output.append(line)
            structured = True
        else:
            paragraph.append(line)
    flush()
    return "".join(output)
