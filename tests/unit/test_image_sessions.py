"""Original image-output retention, rejection recovery and inheritance rules."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from strixops.config.context import ContextSettings
from strixops.engine.sessions import (
    enforce_image_budget,
    open_agent_session,
    scrub_images_from_items,
    session_write_lock,
    strip_all_images_from_session,
)


def image_exchange(index: int, *, images: int = 1) -> list[dict]:
    return [
        {"type": "function_call", "call_id": f"image-{index}", "name": "view_image", "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": f"image-{index}",
            "output": [
                {"type": "input_text", "text": f"screenshot caption {index}"},
                *[
                    {"type": "input_image", "image_url": f"data:image/png;base64,IMAGE_{index}_{part}"}
                    for part in range(images)
                ],
                {"type": "input_text", "text": f"trailing text {index}"},
            ],
        },
    ]


def image_call_ids(items: list[dict]) -> list[str]:
    return [
        item["call_id"]
        for item in items
        if item.get("type") == "function_call_output"
        and isinstance(item.get("output"), list)
        and any(block.get("type") == "input_image" for block in item["output"])
    ]


@pytest.fixture()
def session(tmp_path):
    value = open_agent_session("images", tmp_path / "agents.db")
    yield value
    value.close()


async def test_default_budget_keeps_newest_three_outputs_preserving_text_and_call_pairs(session):
    original = [{"role": "user", "content": "Analyze observed screenshots"}]
    for index in range(5):
        original.extend(image_exchange(index))
    await session.add_items(original)
    assert await enforce_image_budget(session, ContextSettings().max_context_images)
    rebuilt = await session.get_items()
    assert len(rebuilt) == len(original)
    assert image_call_ids(rebuilt) == ["image-2", "image-3", "image-4"]
    assert rebuilt[0] == original[0]
    for index in range(5):
        call, output = rebuilt[1 + index * 2 : 3 + index * 2]
        assert call == original[1 + index * 2]
        assert output["call_id"] == call["call_id"]
        assert output["output"][0] == {"type": "input_text", "text": f"screenshot caption {index}"}
        assert output["output"][-1] == {"type": "input_text", "text": f"trailing text {index}"}
        if index < 2:
            assert output["output"][1] == {
                "type": "input_text",
                "text": "[older screenshot elided to bound context memory]",
            }
        else:
            assert output == original[2 + index * 2]
    assert not await enforce_image_budget(session, 3)


@pytest.mark.parametrize(
    "budget, expected", [(0, []), (-1, ["image-0", "image-1"]), (5, ["image-0", "image-1"])]
)
async def test_zero_budget_elides_all_negative_or_roomy_budget_leaves_history(session, budget, expected):
    items = [*image_exchange(0), *image_exchange(1)]
    await session.add_items(items)
    assert await enforce_image_budget(session, budget) is (budget == 0)
    assert image_call_ids(await session.get_items()) == expected
    if budget != 0:
        assert await session.get_items() == items


async def test_budget_counts_image_tool_outputs_as_in_reference_not_individual_blocks(session):
    items = [*image_exchange(0, images=2), *image_exchange(1, images=3)]
    await session.add_items(items)
    assert await enforce_image_budget(session, 1)
    rebuilt = await session.get_items()
    assert rebuilt[3] == items[3]
    assert sum(block["type"] == "input_image" for block in rebuilt[3]["output"]) == 3
    assert all(block["type"] == "input_text" for block in rebuilt[1]["output"])


async def test_rejection_strip_preserves_sibling_text_calls_and_non_tool_input(session):
    user_image = {
        "role": "user",
        "content": [{"type": "input_image", "image_url": "data:image/png;base64,USER_INPUT"}],
    }
    text_output = {"type": "function_call_output", "call_id": "text-call", "output": "ordinary output"}
    items = [user_image, *image_exchange(0, images=2), text_output, *image_exchange(1)]
    await session.add_items(items)
    assert await strip_all_images_from_session(session)
    rebuilt = await session.get_items()
    assert not image_call_ids(rebuilt)
    assert rebuilt[0] == user_image and rebuilt[3] == text_output
    for index in (2, 5):
        output = rebuilt[index]
        assert output["call_id"] == items[index]["call_id"]
        assert output["output"][0] == items[index]["output"][0]
        assert output["output"][-1] == items[index]["output"][-1]
        assert all(
            block == {"type": "input_text", "text": "[image rejected by the model]"}
            for block in output["output"][1:-1]
        )
    assert not await strip_all_images_from_session(session)


async def test_empty_and_text_only_sessions_do_not_rewrite(session, monkeypatch):
    async def unexpected_clear():
        raise AssertionError("unchanged session must not be rewritten")

    monkeypatch.setattr(session, "clear_session", unexpected_clear)
    assert not await enforce_image_budget(session, 0)
    assert not await strip_all_images_from_session(session)
    await session.add_items([{"role": "user", "content": "text only"}])
    assert not await enforce_image_budget(session, 0)
    assert not await strip_all_images_from_session(session)


@pytest.mark.parametrize("operation", ["budget", "rejection"])
async def test_failed_image_rewrite_rolls_back_history_and_releases_lock(session, monkeypatch, operation):
    original = [{"role": "user", "content": "original task"}, *image_exchange(0)]
    await session.add_items(original)
    add_items = session.add_items
    fail_next_write = True

    async def interrupted_write(items):
        nonlocal fail_next_write
        if fail_next_write:
            fail_next_write = False
            await add_items(items[:1])
            raise OSError("simulated partial image rewrite")
        await add_items(items)

    monkeypatch.setattr(session, "add_items", interrupted_write)
    with pytest.raises(OSError, match="simulated partial image rewrite"):
        if operation == "budget":
            await enforce_image_budget(session, 0)
        else:
            await strip_all_images_from_session(session)
    assert await session.get_items() == original
    assert not session_write_lock(session).locked()


def test_inherited_scrub_copies_nested_images_without_mutating_source():
    items = [
        {"role": "user", "content": [{"type": "input_image", "image_url": "USER_IMAGE"}]},
        *image_exchange(0),
        {"metadata": {"nested": [{"type": "input_image", "image_url": "NESTED_IMAGE"}]}},
    ]
    original = copy.deepcopy(items)
    scrubbed = scrub_images_from_items(items)
    marker = {"type": "input_text", "text": "[screenshot omitted from inherited context]"}
    assert scrubbed[0]["content"] == [marker]
    assert scrubbed[2]["output"][1] == marker
    assert scrubbed[3]["metadata"]["nested"] == [marker]
    assert items == original
    scrubbed[0]["content"].append({"type": "input_text", "text": "child-only"})
    assert items == original


def test_image_budget_configuration_uses_original_environment_alias(monkeypatch):
    monkeypatch.setenv("STRIX_MAX_CONTEXT_IMAGES", "7")
    assert ContextSettings().max_context_images == 7
    assert ContextSettings(max_context_images=0).max_context_images == 0
    with pytest.raises(ValidationError):
        ContextSettings(max_context_images=-1)
