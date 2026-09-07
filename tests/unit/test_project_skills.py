"""Project skill analytics and dispatch/load de-duplication."""

from __future__ import annotations

import json
from pathlib import Path

from strixops.console.project_skills import aggregate_project_skill_analytics


def _event(event_type: str, payload: dict, *, actor_id: str = "root", timestamp: str = "t0") -> dict:
    return {
        "timestamp": timestamp,
        "event_type": event_type,
        "actor": {"agent_id": actor_id, "agent_name": actor_id},
        "payload": payload,
    }


def _write_run(run_dir: Path, *, events: list[dict], status: str = "completed") -> None:
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "status": status,
                "start_time": "2026-09-05T10:00:00Z",
                "scan_config": {
                    "target": "https://record.example",
                    "project_id": "prj_test",
                },
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\nnot-json\n",
        encoding="utf-8",
    )


def test_project_skill_analytics_deduplicates_dispatch_side(tmp_path: Path) -> None:
    run_one = tmp_path / "run_one"
    _write_run(
        run_one,
        events=[
            _event(
                "run.configured",
                {"scan_config": {"target": "https://event.example"}},
                timestamp="2026-09-05T09:59:00Z",
            ),
            # create_agent dispatch: do not count because the authoritative
            # agent.created event below represents the same injection.
            _event(
                "tool.execution.started",
                {
                    "args": {
                        "name": "sqli",
                        "task": "test SQL injection",
                        "skills": ["vulnerabilities/sql_injection"],
                    }
                },
            ),
            _event(
                "agent.created",
                {"skills": ["vulnerabilities/sql_injection", "vulnerabilities/sql_injection"]},
                actor_id="child-1",
            ),
            # Replayed agent.created event is an obvious duplicate.
            _event(
                "agent.created",
                {"skills": ["vulnerabilities/sql_injection"]},
                actor_id="child-1",
            ),
            # True load_skill calls remain separate hits. Duplicate names in
            # one call are counted once.
            _event(
                "tool.execution.started",
                {
                    "args": {
                        "skills": [
                            "vulnerabilities/sql_injection",
                            "tooling/httpx",
                            "tooling/httpx",
                        ]
                    }
                },
            ),
            _event(
                "tool.execution.started",
                {"args": {"skills": ["vulnerabilities/sql_injection"]}},
            ),
        ],
    )
    run_two = tmp_path / "run_two"
    _write_run(run_two, events=[])

    result = aggregate_project_skill_analytics([run_two, run_one, run_one], top_limit=1)

    assert result["totals"] == {
        "total_hits": 4,
        "total_loads": 4,
        "distinct_skills": 2,
        "run_count": 2,
        "project_runs": 2,
        "runs_with_skills": 1,
    }
    assert result["top_skills"] == [
        {
            "skill": "vulnerabilities/sql_injection",
            "category": "vulnerabilities",
            "hits": 3,
            "loads": 3,
            "runs": 1,
        }
    ]
    assert result["skills"][1]["skill"] == "tooling/httpx"
    first = next(run for run in result["by_run"] if run["run"] == "run_one")
    assert first["target"] == "https://event.example"
    assert first["skills"] == {
        "tooling/httpx": 1,
        "vulnerabilities/sql_injection": 3,
    }
    assert result["runs"] is result["by_run"]


def test_injected_skill_counts_once_per_agent(tmp_path: Path) -> None:
    run_dir = tmp_path / "multi_agent"
    _write_run(
        run_dir,
        events=[
            _event(
                "agent.created",
                {"skills": ["analysis/counterevidence"]},
                actor_id="child-a",
            ),
            _event(
                "agent.created",
                {"skills": ["analysis/counterevidence"]},
                actor_id="child-b",
            ),
        ],
    )

    result = aggregate_project_skill_analytics([run_dir])

    assert result["totals"]["total_hits"] == 2
    assert result["skills"][0]["runs"] == 1
    assert result["skills"][0]["hits"] == 2
