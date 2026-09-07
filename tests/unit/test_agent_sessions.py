"""SDK history isolation and the reference seed/rewrite guarantees."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from strixops.engine.sessions import (
    open_agent_session,
    replace_session_items,
    seed_initial_input,
    session_write_lock,
)


async def test_agents_share_database_without_sharing_history(tmp_path: Path) -> None:
    path = tmp_path / ".state" / "agents.db"
    root = open_agent_session("root", path)
    child = open_agent_session("child", path)
    try:
        assert await seed_initial_input(root, "root assignment") is True
        assert await seed_initial_input(child, "child assignment") is True
        assert await root.get_items() == [{"role": "user", "content": "root assignment"}]
        assert await child.get_items() == [{"role": "user", "content": "child assignment"}]
        await child.add_items([{"role": "assistant", "content": "child result"}])
        assert len(await root.get_items()) == 1
        assert len(await child.get_items()) == 2
    finally:
        root.close()
        child.close()

    reopened = open_agent_session("child", path)
    try:
        assert await seed_initial_input(reopened, "must not overwrite") is False
        assert await reopened.get_items() == [
            {"role": "user", "content": "child assignment"},
            {"role": "assistant", "content": "child result"},
        ]
    finally:
        reopened.close()


async def test_concurrent_seeding_writes_the_opening_input_once(tmp_path: Path) -> None:
    session = open_agent_session("root", tmp_path / "agents.db")
    try:
        results = await asyncio.gather(
            seed_initial_input(session, "first"), seed_initial_input(session, "second")
        )
        assert sorted(results) == [False, True]
        assert await session.get_items() == [{"role": "user", "content": "first"}]
    finally:
        session.close()


async def test_empty_seed_does_not_claim_session_and_list_seed_preserves_sdk_items(tmp_path: Path) -> None:
    session = open_agent_session("child", tmp_path / "agents.db")
    items = [
        {"role": "user", "content": "task"},
        {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "file contents"},
    ]
    try:
        assert await seed_initial_input(session, []) is False
        assert await seed_initial_input(session, items) is True
        assert await session.get_items() == items
    finally:
        session.close()


async def test_stale_summary_does_not_overwrite_new_session_items(tmp_path: Path) -> None:
    session = open_agent_session("root", tmp_path / "agents.db")
    original = [{"role": "user", "content": "task"}]
    hint = {"role": "user", "content": "new operator instruction"}
    try:
        await session.add_items(original)
        await session.add_items([hint])
        assert (
            await replace_session_items(
                session, [{"role": "user", "content": "obsolete summary"}], expected_len=1
            )
            is False
        )
        assert await session.get_items() == [*original, hint]
        assert await replace_session_items(session, [hint], expected_len=2) is True
        assert await session.get_items() == [hint]
    finally:
        session.close()


class PartialWriteFailure:
    def __init__(self) -> None:
        self.items = [{"role": "user", "content": "original task"}]
        self.fail_next_add = True

    async def get_items(self) -> list:
        return list(self.items)

    async def clear_session(self) -> None:
        self.items.clear()

    async def add_items(self, items: list) -> None:
        self.items.extend(items[:1])
        if self.fail_next_add:
            self.fail_next_add = False
            raise OSError("simulated interrupted database write")
        self.items.extend(items[1:])


async def test_rewrite_failure_restores_original_items() -> None:
    session = PartialWriteFailure()
    original = list(session.items)

    with pytest.raises(OSError, match="simulated interrupted database write"):
        await replace_session_items(session, [{"role": "user", "content": "new summary"}])

    assert session.items == original
    assert not session_write_lock(session).locked()


async def test_write_locks_are_scoped_to_each_agent_session(tmp_path: Path) -> None:
    root = open_agent_session("root", tmp_path / "agents.db")
    child = open_agent_session("child", tmp_path / "agents.db")
    try:
        assert session_write_lock(root) is session_write_lock(root)
        assert session_write_lock(root) is not session_write_lock(child)
        async with session_write_lock(root):
            assert await seed_initial_input(child, "independent child") is True
    finally:
        root.close()
        child.close()


async def test_closed_session_rejects_further_database_access(tmp_path: Path) -> None:
    session = open_agent_session("root", tmp_path / "agents.db")
    await seed_initial_input(session, "task")
    session.close()

    with pytest.raises(RuntimeError, match="closed"):
        await session.get_items()
