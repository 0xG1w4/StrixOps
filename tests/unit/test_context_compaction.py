"""Reference context settings, budget calculations and session compaction contracts."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from agents import ModelResponse, ModelTracing
from agents.usage import Usage
from openai import BadRequestError, RateLimitError
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic import ValidationError

from strixops.config.context import ContextSettings
from strixops.engine import compaction, context_budget


class MemorySession:
    def __init__(self, items: list | None = None) -> None:
        self.items = list(items or [])
        self.writes = 0

    async def get_items(self, limit: int | None = None) -> list:
        return list(self.items) if limit is None else list(self.items[-limit:])

    async def clear_session(self) -> None:
        self.items.clear()
        self.writes += 1

    async def add_items(self, items: list) -> None:
        self.items.extend(items)
        self.writes += 1


def _response(text: str = "## Objective\nKeep the verified facts and continue.") -> ModelResponse:
    return ModelResponse(
        output=[
            ResponseOutputMessage(
                id="summary",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
                type="message",
            )
        ],
        usage=Usage(),
        response_id="local-summary",
    )


def _history(turns: int = 10) -> list[dict]:
    items = [{"role": "user", "content": "Inspect the assigned local fixture"}]
    for number in range(turns):
        items.extend(
            [
                {
                    "type": "function_call",
                    "call_id": f"c{number}",
                    "name": "exec_command",
                    "arguments": '{"cmd":"inspect fixture"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": f"c{number}",
                    "output": f"observed-{number}: " + "evidence " * 30,
                },
                {"role": "assistant", "content": f"Verified observation {number}"},
            ]
        )
    return items


@pytest.fixture(autouse=True)
def isolated_context_settings(monkeypatch: pytest.MonkeyPatch):
    for field in ContextSettings.model_fields.values():
        monkeypatch.delenv(field.alias, raising=False)
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    context_budget._model_info.cache_clear()
    yield
    context_budget._model_info.cache_clear()


@pytest.fixture()
def compact_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(compaction, "count_tokens", lambda _model, text: len(text))
    monkeypatch.setattr(compaction, "context_window", lambda _model, _settings=None: 12_000)
    monkeypatch.setattr(compaction, "output_limit", lambda _model: 512)
    settings = ContextSettings(compact_buffer_tokens=1_000, keep_tokens=500, summary_max_tokens=256)
    model = SimpleNamespace(get_response=AsyncMock(return_value=_response()), close=AsyncMock())
    return SimpleNamespace(settings=settings, model=model)


def test_original_context_defaults_and_environment_names(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ContextSettings().model_dump() == {
        "auto_compact": True,
        "compact_buffer_tokens": 20_000,
        "keep_tokens": 8_000,
        "fallback_context_tokens": 200_000,
        "summary_max_tokens": 4_096,
        "tool_output_max_tokens": 8_000,
        "tool_output_max_lines": 2_000,
        "tool_output_max_bytes": 51_200,
        "max_context_images": 3,
    }
    monkeypatch.setenv("STRIX_CONTEXT_AUTO_COMPACT", "false")
    monkeypatch.setenv("STRIX_CONTEXT_BUFFER_TOKENS", "1234")
    monkeypatch.setenv("STRIX_CONTEXT_KEEP_TOKENS", "678")
    monkeypatch.setenv("STRIX_CONTEXT_FALLBACK_TOKENS", "16000")
    monkeypatch.setenv("STRIX_CONTEXT_SUMMARY_TOKENS", "256")
    configured = ContextSettings()
    assert configured.auto_compact is False
    assert configured.compact_buffer_tokens == 1234
    assert configured.keep_tokens == 678
    assert configured.fallback_context_tokens == 16000
    assert configured.summary_max_tokens == 256


@pytest.mark.parametrize(
    "overrides", [{"keep_tokens": 0}, {"summary_max_tokens": -1}, {"tool_output_max_bytes": 1023}]
)
def test_context_settings_reject_invalid_original_limits(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        ContextSettings(**overrides)


def test_budget_metadata_lookup_uses_original_prefix_fallback_and_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = Mock(side_effect=[None, {"max_input_tokens": 32000, "max_output_tokens": 2048}])
    monkeypatch.setattr(context_budget, "_safe_get_model_info", lookup)

    assert context_budget.context_window("openai/local-model") == 32000
    assert context_budget.output_limit("openai/local-model") == 2048
    assert [call.args[0] for call in lookup.call_args_list] == ["openai/local-model", "local-model"]


def test_chatgpt_metadata_does_not_start_provider_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = Mock(return_value={"max_tokens": 4096})
    monkeypatch.setattr(context_budget, "_safe_get_model_info", lookup)
    assert context_budget.context_window("chatgpt/local-model") == 4096
    lookup.assert_called_once_with("local-model")


def test_unmapped_model_uses_configured_capacity_and_original_output_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context_budget, "_safe_get_model_info", Mock(return_value=None))
    settings = ContextSettings(fallback_context_tokens=72000)
    assert context_budget.context_window("unknown", settings) == 72000
    assert context_budget.output_limit("unknown") == 8192


def test_token_count_falls_back_to_utf8_bytes_without_provider_io(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = Mock(side_effect=ValueError("unknown tokenizer"))
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(token_counter=counter))
    assert context_budget.count_tokens("openai/unknown", "測試") == 6
    counter.assert_called_once_with(model="unknown", text="測試")
    assert context_budget.count_tokens("unknown", "") == 0
    assert counter.call_count == 1


@pytest.fixture()
def legacy_error_types(monkeypatch: pytest.MonkeyPatch):
    class ContextWindowExceeded(Exception):
        pass

    class LegacyBadRequest(Exception):
        pass

    monkeypatch.setattr(
        compaction, "_overflow_error_types", lambda: (ContextWindowExceeded, LegacyBadRequest)
    )
    return ContextWindowExceeded, LegacyBadRequest


@pytest.mark.parametrize(
    "message",
    [
        "maximum context length exceeded",
        "input is too long",
        "token limit exceeded",
        "request entity too large",
    ],
)
def test_openai_bad_request_overflow_adapter(legacy_error_types, message: str) -> None:
    response = httpx.Response(400, request=httpx.Request("POST", "https://fixture.invalid/v1"))
    assert compaction.is_context_overflow(BadRequestError(message, response=response, body=None))


@pytest.mark.parametrize(
    "message",
    [
        "quota: context length exceeded",
        "rate limit: too many tokens",
        "service unavailable: input is too long",
        "invalid tool schema",
    ],
)
def test_openai_bad_request_exclusions_preserve_reference_semantics(legacy_error_types, message: str) -> None:
    response = httpx.Response(400, request=httpx.Request("POST", "https://fixture.invalid/v1"))
    assert not compaction.is_context_overflow(BadRequestError(message, response=response, body=None))


def test_typed_legacy_overflow_and_non_overflow_errors(legacy_error_types) -> None:
    typed, legacy = legacy_error_types
    assert compaction.is_context_overflow(typed("typed overflow"))
    assert compaction.is_context_overflow(legacy("context_length_exceeded"))
    assert not compaction.is_context_overflow(RuntimeError("context length exceeded"))
    response = httpx.Response(429, request=httpx.Request("POST", "https://fixture.invalid/v1"))
    assert not compaction.is_context_overflow(RateLimitError("too many tokens", response=response, body=None))


def test_token_tail_split_keeps_parallel_tool_calls_with_their_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compaction, "count_tokens", lambda _model, _text: 1)
    items = [
        {"role": "user", "content": "task"},
        {"type": "function_call", "call_id": "a", "name": "a", "arguments": "{}"},
        {"type": "function_call", "call_id": "b", "name": "b", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "a", "output": "a result"},
        {"type": "function_call_output", "call_id": "b", "output": "b result"},
        {"role": "assistant", "content": "done"},
    ]
    assert compaction._select_split("fixture", items, keep_tokens=2) == 1


@pytest.mark.parametrize("disabled", [False, True])
async def test_in_budget_or_disabled_compaction_does_not_call_model(compact_env, disabled: bool) -> None:
    original = _history(3)
    session = MemorySession(original)
    settings = compact_env.settings.model_copy(update={"auto_compact": not disabled})

    assert (
        await compaction.maybe_compact(
            session, model="fixture", summary_model=compact_env.model, settings=settings
        )
        is False
    )
    compact_env.model.get_response.assert_not_awaited()
    assert session.items == original and session.writes == 0


async def test_force_compacts_even_when_auto_disabled_and_retains_complete_recent_turns(compact_env) -> None:
    original = _history()
    session = MemorySession(original)
    settings = compact_env.settings.model_copy(update={"auto_compact": False})

    assert (
        await compaction.maybe_compact(
            session, model="fixture", summary_model=compact_env.model, settings=settings, force=True
        )
        is True
    )

    assert session.items[0]["role"] == "user"
    assert session.items[0]["content"].startswith("<conversation-checkpoint>")
    assert session.items[-1] == original[-1]
    calls = {item["call_id"] for item in session.items if item.get("type") == "function_call"}
    assert all(
        item["call_id"] in calls for item in session.items if item.get("type") == "function_call_output"
    )
    compact_env.model.get_response.assert_awaited_once()
    kwargs = compact_env.model.get_response.call_args.kwargs
    assert kwargs["tools"] == kwargs["handoffs"] == []
    assert kwargs["system_instructions"] is kwargs["output_schema"] is None
    assert kwargs["previous_response_id"] is kwargs["conversation_id"] is kwargs["prompt"] is None
    assert kwargs["tracing"] == ModelTracing.DISABLED
    assert kwargs["model_settings"].max_tokens == 256
    assert kwargs["model_settings"].include_usage is True
    assert kwargs["model_settings"].parallel_tool_calls is None
    assert kwargs["model_settings"].reasoning is None
    assert kwargs["model_settings"].extra_args == {"timeout": 300.0}
    for heading in (
        "Objective",
        "Vulnerabilities & Findings",
        "Credentials & Secrets",
        "System & Recon Details",
        "Work State",
        "Failed Attempts & Dead Ends",
        "Next Move",
        "Relevant Files",
    ):
        assert f"## {heading}" in kwargs["input"]
    compact_env.model.close.assert_not_awaited()


@pytest.mark.parametrize("count", [0, 1, 5])
async def test_original_minimum_six_items_applies_even_when_forced(compact_env, count: int) -> None:
    session = MemorySession(_history()[:count])
    assert (
        await compaction.maybe_compact(
            session,
            model="fixture",
            summary_model=compact_env.model,
            force=True,
            settings=compact_env.settings,
        )
        is False
    )
    compact_env.model.get_response.assert_not_awaited()


async def test_budget_trigger_includes_instructions_and_tool_definitions(compact_env) -> None:
    session = MemorySession(_history(3))
    assert (
        await compaction.maybe_compact(
            session,
            model="fixture",
            summary_model=compact_env.model,
            settings=compact_env.settings,
            instructions="instructions " * 500,
            tools_text="tool-schema " * 500,
        )
        is True
    )


async def test_summary_updates_previous_checkpoint_and_honors_output_cap(compact_env, monkeypatch) -> None:
    previous = compaction._checkpoint_item("Previous verified finding")
    session = MemorySession([previous, *_history()])
    monkeypatch.setattr(compaction, "output_limit", lambda _model: 64)
    monkeypatch.setenv("LLM_TIMEOUT", "42")
    assert (
        await compaction.maybe_compact(
            session,
            model="fixture",
            summary_model=compact_env.model,
            force=True,
            settings=compact_env.settings,
        )
        is True
    )
    kwargs = compact_env.model.get_response.call_args.kwargs
    assert "A previous checkpoint summary follows. Update it" in kwargs["input"]
    assert "Previous verified finding" in kwargs["input"]
    assert kwargs["model_settings"].max_tokens == 64
    assert kwargs["model_settings"].extra_args == {"timeout": 42.0}


@pytest.mark.parametrize("failure", [RuntimeError("provider unavailable"), ""])
async def test_summary_failure_or_empty_response_never_rewrites_history(compact_env, failure) -> None:
    original = _history()
    session = MemorySession(original)
    if isinstance(failure, Exception):
        compact_env.model.get_response.side_effect = failure
    else:
        compact_env.model.get_response.return_value = _response(failure)
    assert (
        await compaction.maybe_compact(
            session,
            model="fixture",
            summary_model=compact_env.model,
            force=True,
            settings=compact_env.settings,
        )
        is False
    )
    assert session.items == original and session.writes == 0


async def test_message_arriving_during_summary_prevents_stale_rewrite(compact_env) -> None:
    original = _history()
    session = MemorySession(original)
    hint = {"role": "user", "content": "New instruction while summary was running"}

    async def summarize(**kwargs):
        await session.add_items([hint])
        return _response()

    compact_env.model.get_response.side_effect = summarize
    assert (
        await compaction.maybe_compact(
            session,
            model="fixture",
            summary_model=compact_env.model,
            force=True,
            settings=compact_env.settings,
        )
        is False
    )
    assert session.items == [*original, hint]


async def test_summary_cancellation_propagates_without_rewriting(compact_env) -> None:
    original = _history()
    session = MemorySession(original)
    compact_env.model.get_response.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await compaction.maybe_compact(
            session,
            model="fixture",
            summary_model=compact_env.model,
            force=True,
            settings=compact_env.settings,
        )
    assert session.items == original and session.writes == 0


def test_summary_serialization_preserves_original_output_cap_and_omits_reasoning() -> None:
    text = compaction._serialize_items(
        [
            {"type": "function_call_output", "call_id": "x", "output": "x" * 5000},
            {"type": "reasoning", "content": "hidden reasoning"},
        ]
    )
    assert "x" * 2000 in text and "x" * 2001 not in text
    assert "[truncated]" in text
    assert "hidden reasoning" not in text


def test_summary_input_fitting_retains_head_and_tail_with_original_notice(monkeypatch) -> None:
    monkeypatch.setattr(compaction, "count_tokens", lambda _model, text: len(text))
    result = compaction._fit_to_tokens("fixture", "BEGIN" + "x" * 5000 + "END", 300)
    assert len(result) <= 300
    assert result.startswith("BEGIN") and result.endswith("END")
    assert "older conversation omitted" in result


async def test_no_summary_room_keeps_session_intact(compact_env, monkeypatch) -> None:
    original = _history()
    session = MemorySession(original)
    monkeypatch.setattr(compaction, "context_window", lambda _model, _settings=None: 100)
    assert (
        await compaction.maybe_compact(
            session,
            model="fixture",
            summary_model=compact_env.model,
            force=True,
            settings=compact_env.settings,
        )
        is False
    )
    compact_env.model.get_response.assert_not_awaited()
    assert session.items == original
