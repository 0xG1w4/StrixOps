"""Original head/tail output previews with an in-memory sandbox spill writer."""

from __future__ import annotations

import re

import pytest

from strixops.tools.output_store import (
    WORKSPACE_SPILL_DIR,
    bound_and_store,
    bound_text,
    configure_spill_writer,
)


@pytest.fixture(autouse=True)
def clean_writer():
    configure_spill_writer(None)
    yield
    configure_spill_writer(None)


async def test_small_output_is_returned_unchanged_without_spilling():
    calls = []

    async def writer(identity, text):
        calls.append((identity, text))
        return f"{WORKSPACE_SPILL_DIR}/{identity}.txt"

    configure_spill_writer(writer)
    text = '{"success":true,"message":"ok"}'
    assert await bound_and_store(text, max_lines=2000, max_bytes=50 * 1024) == text
    assert calls == []


async def test_line_limit_keeps_head_tail_and_spills_complete_result():
    text = "\n".join(f"line {index:03d}" for index in range(100))
    stored = {}

    async def writer(identity, contents):
        assert re.fullmatch(r"[0-9a-f]{32}", identity)
        path = f"{WORKSPACE_SPILL_DIR}/{identity}.txt"
        stored[path] = contents
        return path

    configure_spill_writer(writer)
    result = await bound_and_store(text, max_lines=10, max_bytes=2048)
    assert len(stored) == 1
    path, contents = next(iter(stored.items()))
    assert contents == text
    assert result.startswith("line 000\nline 001")
    assert result.endswith("line 098\nline 099")
    assert "line 050" not in result
    assert "90 lines" in result
    assert path in result
    assert "read it with exec_command" in result


def test_byte_limit_handles_unicode_without_splitting_characters():
    text = "開始" + "資料🦊" * 1000 + "結束"
    result = bound_text(text, max_lines=2000, max_bytes=1024)
    assert len(result.encode("utf-8")) <= 1024
    assert result.startswith("開始")
    assert result.endswith("結束")
    assert "truncated" in result
    assert "\ufffd" not in result


async def test_unavailable_spill_uses_plain_preview():
    async def unavailable(_identity, _text):
        return None

    configure_spill_writer(unavailable)
    result = await bound_and_store("x" * 10000, max_lines=2000, max_bytes=1024)
    assert len(result.encode()) <= 1024
    assert "truncated" in result
    assert "saved" not in result


async def test_clearing_writer_stops_spills():
    async def unexpected(_identity, _text):
        raise AssertionError("cleared writer must not be called")

    configure_spill_writer(unexpected)
    configure_spill_writer(None)
    result = await bound_and_store("x" * 10000, max_lines=2000, max_bytes=1024)
    assert "truncated" in result
    assert "saved" not in result
