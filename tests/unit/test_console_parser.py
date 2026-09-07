"""Console-side parser regressions — hint-echo ack tokens and conversation."""

from __future__ import annotations

import json
from pathlib import Path

from strixops.console import parser


def _write_events(path: Path, *events: dict) -> None:
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def _chat(content: str) -> dict:
    return {
        "timestamp": "2026-09-03T00:00:00+00:00",
        "event_type": "chat.message",
        "actor": {"agent_id": "root", "agent_name": "root agent"},
        "payload": {"content": content},
    }


def test_hint_ack_accepts_full_token_alphabet(tmp_path: Path) -> None:
    """Mixed-alphanumeric tokens (the scripted fixture's ``echo12345``) must
    ack — the old hex-only class silently dropped them."""
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        _chat("[hint:echo12345] acknowledged"),
        _chat("[hint:abcd1234] acknowledged"),
        _chat("no token here"),
    )
    assert parser.hint_ack_tokens(events) == {"echo12345", "abcd1234"}


def test_read_conversation_cursor_and_types(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        {"timestamp": "t0", "event_type": "run.configured", "actor": {}, "payload": {}},
        _chat("hello"),
        {"timestamp": "t2", "event_type": "unknown.event", "actor": {}, "payload": {}},
    )
    messages, cursor = parser.read_conversation(events, after=-1)
    assert [m["type"] for m in messages] == ["system", "message"]
    assert cursor == 2  # advances past the unknown event too
    tail, next_cursor = parser.read_conversation(events, after=cursor)
    assert tail == [] and next_cursor == cursor
