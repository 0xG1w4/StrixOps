"""Offline input-budget checks for complete report-seeded assessments."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from types import FunctionType
from typing import Any

from strixops.config.context import ContextSettings
from strixops.config.model_options import resolved_api_mode
from strixops.config.provider import strip_provider_prefix
from strixops.console.rerun_context import ContinuationError
from strixops.engine.scanconfig import (
    PREVIOUS_REPORT_END,
    PREVIOUS_REPORT_START,
    ScanSpec,
    build_root_task,
)

_MODEL_PREFIXES = {"openai", "anthropic", "google", "chatgpt", "litellm", "any-llm"}
_MESSAGE_RESERVE = 2_048


@lru_cache(maxsize=1)
def _local_model_map() -> dict:
    """Read the bundled catalog without importing LiteLLM's network loader."""
    try:
        package = importlib.util.find_spec("litellm")
        if package is None or package.origin is None:
            return {}
        path = Path(package.origin).with_name("model_prices_and_context_window_backup.json")
        if path.stat().st_size > 32 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ImportError, OSError, ValueError):
        return {}


def _positive_int(value: Any) -> int:
    return value if type(value) is int and value > 0 else 0


def _capacity(model: str, settings: ContextSettings) -> tuple[int, int, str]:
    names = [model, strip_provider_prefix(model)]
    for name in list(names):
        prefix, separator, suffix = name.partition("/")
        if separator and prefix in _MODEL_PREFIXES:
            names.append(suffix)
    catalog = _local_model_map()
    for name in dict.fromkeys(names):
        row = catalog.get(name)
        if not isinstance(row, dict):
            continue
        capacity = _positive_int(row.get("max_input_tokens")) or _positive_int(row.get("max_tokens"))
        if capacity:
            return capacity, _positive_int(row.get("max_output_tokens")) or 8_192, "bundled_catalog"
    return settings.fallback_context_tokens, 8_192, "configured_fallback"


def _load_cached_bpe(url: str, expected_hash: str | None = None) -> dict[bytes, int]:
    directory = os.environ.get("TIKTOKEN_CACHE_DIR", os.environ.get("DATA_GYM_CACHE_DIR"))
    if directory is None:
        directory = str(Path(tempfile.gettempdir()) / "data-gym-cache")
    if not directory or not expected_hash:
        raise ValueError("No verified local tokenizer cache")
    path = Path(directory) / hashlib.sha1(url.encode()).hexdigest()
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Tokenizer cache exceeds read limit")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError("Tokenizer cache digest mismatch")
    return {
        base64.b64decode(token, validate=True): int(rank)
        for line in data.splitlines() if line
        for token, rank in [line.split()]
    }


@lru_cache(maxsize=8)
def _cached_encoding(name: str, _cache_directory: str | None) -> Any:
    try:
        import tiktoken
        from tiktoken import registry
        from tiktoken_ext import openai_public

        if name in registry.ENCODINGS:
            return registry.ENCODINGS[name]
        constructor = openai_public.ENCODING_CONSTRUCTORS[name]
        # Give this invocation a private, strictly local BPE loader. Neither
        # the process environment nor tiktoken's shared loader is modified.
        local = FunctionType(
            constructor.__code__,
            {**constructor.__globals__, "load_tiktoken_bpe": _load_cached_bpe},
            constructor.__name__, constructor.__defaults__, constructor.__closure__,
        )
        return tiktoken.Encoding(**local())
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return None


def _counter(model: str):
    cache = os.environ.get("TIKTOKEN_CACHE_DIR", os.environ.get("DATA_GYM_CACHE_DIR"))
    try:
        from tiktoken.model import encoding_name_for_model

        name = encoding_name_for_model(model.rsplit("/", 1)[-1])
    except (ImportError, KeyError):
        name = ""
    if name in {"cl100k_base", "o200k_base"} and (encoding := _cached_encoding(name, cache)):
        return lambda text: len(encoding.encode(text, disallowed_special=())), name
    # Other providers do not expose the same tokenizer. Use the larger local
    # estimate plus 25% headroom, keeping UTF-8 bytes as the no-cache fallback.
    encodings = [
        value for name in ("cl100k_base", "o200k_base")
        if (value := _cached_encoding(name, cache)) is not None
    ]
    if encodings:
        def estimate(text: str) -> int:
            return math.ceil(max(len(enc.encode(text, disallowed_special=())) for enc in encodings) * 1.25)

        return estimate, "local_tokenizer_estimate_with_margin"
    return lambda text: len(text.encode("utf-8")), "utf8_upper_bound"


class _SchemaSession:
    """Only tool construction uses this object; it cannot execute anything."""

    def supports_pty(self) -> bool:
        return True


@dataclass
class _BudgetSpec(ScanSpec):
    report_markdown: str = ""

    def load_previous_report(self) -> str:
        return self.report_markdown


def _system_material(instructions: str) -> str:
    from agents.sandbox.capabilities.shell import _SHELL_INSTRUCTIONS
    from agents.sandbox.runtime_agent_preparation import get_default_sandbox_instructions

    return "\n\n".join((get_default_sandbox_instructions() or "", instructions, _SHELL_INSTRUCTIONS))


def _root_material(spec: ScanSpec, api_mode: str) -> tuple[str, str]:
    from agents.models.chatcmpl_converter import Converter as ChatConverter
    from agents.models.openai_responses import Converter as ResponsesConverter

    from strixops.agents.factory import build_root_agent

    agent = build_root_agent(spec, sandbox=True)
    tools = list(agent.tools)
    for capability in agent.capabilities:
        capability.bind(_SchemaSession())
        tools.extend(capability.tools())
    converted = (
        ResponsesConverter.convert_tools(tools, []).tools
        if api_mode == "responses" else [ChatConverter.tool_to_openai(tool) for tool in tools]
    )
    # The additional reserve covers the filesystem manifest, filesystem
    # capability instruction, section headers and provider message framing.
    return (
        _system_material(agent.instructions),
        json.dumps(converted, ensure_ascii=False, separators=(",", ":")),
    )


def _shared_counts(texts: list[str], count: Any) -> list[int]:
    """Count common prompt text once; target scope usually forms the middle."""
    prefix = os.path.commonprefix(texts)
    remaining = [text[len(prefix):] for text in texts]
    suffix = os.path.commonprefix([text[::-1] for text in remaining])[::-1]
    shared = count(prefix) + count(suffix)
    unique: dict[str, int] = {}
    result = []
    for text in remaining:
        middle = text[:-len(suffix)] if suffix else text
        if middle not in unique:
            unique[middle] = count(middle)
        result.append(shared + unique[middle])
    return result


def _budget_spec(spec: ScanSpec, markdown: str = "") -> _BudgetSpec:
    values = asdict(spec)
    values["previous_report_file"] = ""
    values["continuation"] = spec.continuation or {
        "source_run": "x" * 100,
        "report_sha256": "0" * 64,
        "snapshot_file": "previous_report.md",
    }
    return _BudgetSpec(**values, report_markdown=markdown)


def validate_batch_budget(specs: list[ScanSpec], markdown: str, llm_env: dict) -> dict:
    """Check every target while counting shared report, tools and prompts once."""
    from strixops.agents.prompts import root_instructions

    if not specs:
        raise ValueError("At least one scan specification is required")
    model = str(llm_env.get("strix_llm") or "").strip()
    settings = ContextSettings()
    capacity, output_limit, capacity_source = _capacity(model, settings)
    count, counting_method = _counter(model)
    prepared = [_budget_spec(spec) for spec in specs]
    mode = resolved_api_mode(model, str(llm_env.get("llm_api_mode") or "chat_completions"))
    first_system, tools = _root_material(prepared[0], mode)
    systems = [first_system, *[_system_material(root_instructions(spec)) for spec in prepared[1:]]]
    before, after, report_counts = [], [], []
    references: dict[tuple[str, str], int] = {}
    for spec in prepared:
        root_input = build_root_task(spec)
        prefix, marker, tail = root_input.rpartition(PREVIOUS_REPORT_START)
        _, end_marker, suffix = tail.partition(PREVIOUS_REPORT_END)
        if not marker or not end_marker:
            raise ValueError("Previous report reference is missing from the root task")
        before.append(prefix)
        after.append(suffix)
        metadata = spec.continuation
        key = (metadata["source_run"], metadata["report_sha256"])
        if key not in references:
            previous = PREVIOUS_REPORT_START + json.dumps({
                "source_run": key[0], "report_sha256": key[1], "markdown": markdown,
            }, ensure_ascii=False) + PREVIOUS_REPORT_END
            references[key] = count(previous)
        report_counts.append(references[key])
    system_counts = _shared_counts(systems, count)
    before_counts, after_counts = _shared_counts(before, count), _shared_counts(after, count)
    root_counts = [left + report + right for left, report, right in zip(
        before_counts, report_counts, after_counts, strict=True,
    )]
    worst = max(range(len(specs)), key=lambda index: system_counts[index] + root_counts[index])
    output_reserve = max(settings.compact_buffer_tokens, output_limit)
    metrics = {
        "model": model,
        "capacity_tokens": capacity,
        "capacity_source": capacity_source,
        "counting_method": counting_method,
        "target_count": len(specs),
        "system_tokens": system_counts[worst],
        "root_input_tokens": root_counts[worst],
        "report_tokens": report_counts[worst],
        "tool_tokens": count(tools),
        "output_reserve_tokens": output_reserve,
        "overhead_reserve_tokens": _MESSAGE_RESERVE,
    }
    metrics["estimated_input_tokens"] = (
        metrics["system_tokens"] + metrics["root_input_tokens"] + metrics["tool_tokens"] + _MESSAGE_RESERVE
    )
    metrics["available_input_tokens"] = max(0, capacity - output_reserve)
    if metrics["estimated_input_tokens"] > metrics["available_input_tokens"]:
        raise ContinuationError("context_budget_exceeded")
    return metrics


def validate_budget(spec: ScanSpec, markdown: str, llm_env: dict) -> dict:
    """Reject an oversized full report before any run directory or model exists.

    The result is an estimate, not provider usage. API schema transformation
    and unmapped models can differ, so preserve room beyond counted text.
    """
    return validate_batch_budget([spec], markdown, llm_env)
