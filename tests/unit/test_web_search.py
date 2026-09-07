"""Web search preserves source references and fails gracefully without live API calls."""

from __future__ import annotations

import importlib
import json
from unittest.mock import Mock

import pytest
import requests
from agents.tool_context import ToolContext

search_module = importlib.import_module("strixops.tools.web_search")


@pytest.fixture()
def post(monkeypatch: pytest.MonkeyPatch) -> Mock:
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-perplexity-key")
    mocked = Mock()
    monkeypatch.setattr(search_module.requests, "post", mocked)
    return mocked


async def _search(query: str) -> dict:
    arguments = json.dumps({"query": query})
    context = ToolContext(
        context=None,
        tool_name="web_search",
        tool_call_id="test-search",
        tool_arguments=arguments,
    )
    result = await search_module.web_search.on_invoke_tool(context, arguments)
    return json.loads(result)


@pytest.mark.parametrize("key", [None, "   "])
async def test_missing_key_does_not_call_api(post, monkeypatch, key):
    if key is None:
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    else:
        monkeypatch.setenv("PERPLEXITY_API_KEY", key)

    result = await _search("CVE mitigation")

    assert result["success"] is False
    assert "not configured" in result["error"]
    assert "Proceed without it" in result["error"]
    post.assert_not_called()


async def test_empty_query_does_not_call_api(post):
    result = await _search("  \n ")

    assert result == {"success": False, "error": "Query cannot be empty"}
    post.assert_not_called()


async def test_answer_keeps_all_numbered_citation_sources(post):
    citations = [f"https://example.invalid/advisory/{number}" for number in range(1, 8)]
    answer = "The vendor confirms the affected versions [6] and mitigation [7]."
    post.return_value.json.return_value = {
        "choices": [{"message": {"content": answer}}],
        "citations": citations,
    }

    result = await _search("  product vulnerability mitigation  ")

    assert result == {"success": True, "answer": answer, "citations": citations}
    assert result["citations"][6] == "https://example.invalid/advisory/7"
    post.assert_called_once()
    url = post.call_args.args[0]
    request = post.call_args.kwargs
    assert url == "https://api.perplexity.ai/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer test-perplexity-key"
    assert request["json"]["model"] == "sonar"
    assert request["json"]["messages"][0]["role"] == "system"
    assert "cybersecurity" in request["json"]["messages"][0]["content"]
    assert request["json"]["messages"][1] == {
        "role": "user",
        "content": "product vulnerability mitigation",
    }
    post.return_value.raise_for_status.assert_called_once()


async def test_response_without_citations_keeps_answer(post):
    post.return_value.json.return_value = {
        "choices": [{"message": {"content": "No matching vulnerability found."}}],
    }

    result = await _search("product vulnerability")

    assert result == {
        "success": True,
        "answer": "No matching vulnerability found.",
        "citations": [],
    }


@pytest.mark.parametrize(
    ("failure", "error_text"),
    [
        (requests.Timeout(), "timed out"),
        (requests.HTTPError("401 Unauthorized"), "401 Unauthorized"),
        (requests.ConnectionError("connection unavailable"), "connection unavailable"),
    ],
)
async def test_request_errors_return_tool_results(post, failure, error_text):
    if isinstance(failure, requests.HTTPError):
        post.return_value.raise_for_status.side_effect = failure
    else:
        post.side_effect = failure

    result = await _search("CVE mitigation")

    assert result["success"] is False
    assert error_text in result["error"]
