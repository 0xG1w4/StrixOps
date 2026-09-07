"""Capability tools must fail as tool RESULTS, not agent-killing exceptions.

Regression: the first live scan lost two agents to `UserError: … WriteStdinArgs
session_id Field required` — the SDK raised during tool invocation and the run
loop treated it as a fatal turn error. `_error_as_result` returns the error to
the model so it can correct the next call.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from agents import RunContextWrapper
from agents.sandbox.capabilities.shell import WriteStdinTool

from strixops.agents.factory import _error_as_result


def _run(coro):
    return asyncio.run(coro)


def test_missing_required_arg_returns_error_result():
    tool = WriteStdinTool(session=None)
    _error_as_result(tool)
    out = _run(tool.on_invoke_tool(RunContextWrapper(context=None), json.dumps({"chars": "x"})))
    payload = json.loads(out)
    assert "error" in payload
    assert "session_id" in payload["error"]
    assert "hint" in payload


def test_invalid_json_returns_error_result():
    tool = WriteStdinTool(session=None)
    _error_as_result(tool)
    out = _run(tool.on_invoke_tool(RunContextWrapper(context=None), "not json"))
    assert "error" in json.loads(out)


def test_successful_invocation_passthrough(monkeypatch):
    tool = WriteStdinTool(session=None)

    async def ok(ctx, input_json):
        return "all good"

    tool.on_invoke_tool = ok
    _error_as_result(tool)
    out = _run(tool.on_invoke_tool(RunContextWrapper(context=None), "{}"))
    assert out == "all good"


def test_max_turns_default_raised():
    """The first live scan died at 60 turns everywhere; default is now 500
    (STRIXOPS_MAX_TURNS overrides)."""
    from strixops.engine.loop import DEFAULT_MAX_TURNS

    assert DEFAULT_MAX_TURNS == 500


def test_capability_wrapper_preserves_cancellation():
    tool = WriteStdinTool(session=None)

    async def cancelled(ctx, input_json):
        raise asyncio.CancelledError()

    tool.on_invoke_tool = cancelled
    _error_as_result(tool)
    with pytest.raises(asyncio.CancelledError):
        _run(tool.on_invoke_tool(RunContextWrapper(context=None), "{}"))
