"""Prompt and skill editor API contracts with the production static mount."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from strixops import skills
from strixops.agents import prompts
from strixops.console import server


@pytest.fixture()
def content_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    prompt_root = tmp_path / "prompt_parts"
    prompt_root.mkdir()
    (prompt_root / "root_directive.md").write_text("Original prompt\n", encoding="utf-8")
    skill_root = tmp_path / "content"
    (skill_root / "tooling").mkdir(parents=True)
    (skill_root / "tooling" / "python.md").write_text("Original skill\n", encoding="utf-8")
    monkeypatch.setattr(prompts, "PROMPT_PARTS_DIR", prompt_root)
    monkeypatch.setattr(skills, "CONTENT_ROOT", skill_root)

    # Match console startup: the root static mount follows every API route.
    # Copy the route list so fixture cleanup cannot affect another API test.
    monkeypatch.setattr(server.app.router, "routes", list(server.app.router.routes))
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "skills.html").write_text("Editor page", encoding="utf-8")
    server._mount_static(web_root)
    with TestClient(server.app) as client:
        yield client


def test_prompt_editor_put_persists_and_reads_back(content_api: TestClient) -> None:
    entry = content_api.get("/api/prompts").json()["parts"][0]
    endpoint = f"/api/prompts/{entry['name']}"
    assert content_api.get(endpoint).json()["content"] == "Original prompt\n"

    content = "# Updated prompt\n\n測試保存內容。\n"
    response = content_api.put(endpoint, json={"content": content})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "name": "root_directive"}
    assert content_api.get(endpoint).json()["content"] == content
    assert (prompts.PROMPT_PARTS_DIR / entry["file"]).read_text(encoding="utf-8") == content


def test_nested_skill_editor_put_persists_and_reads_back(content_api: TestClient) -> None:
    entry = content_api.get("/api/skills").json()["files"][0]
    params = {"path": entry["path"]}
    assert content_api.get("/api/skills/file", params=params).json()["content"] == "Original skill\n"

    content = "# Updated skill\n\n測試保存內容。\n"
    response = content_api.put("/api/skills/file", params=params, json={"content": content})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": "tooling/python.md"}
    assert content_api.get("/api/skills/file", params=params).json()["content"] == content
    assert (skills.CONTENT_ROOT / entry["path"]).read_text(encoding="utf-8") == content


def test_prompt_post_hits_static_api_404_without_saving(content_api: TestClient) -> None:
    """The old frontend POST explains a 404 despite a registered PUT route."""
    endpoint = "/api/prompts/root_directive"

    response = content_api.post(endpoint, json={"content": "Must not be saved"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert content_api.get(endpoint).json()["content"] == "Original prompt\n"
    assert content_api.get("/skills").status_code == 200
