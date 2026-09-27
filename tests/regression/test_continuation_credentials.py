"""Synthetic credential continuation snapshots; no operator data is read."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime

import pytest

from strixops.console import continuation_credentials as snapshots
from strixops.report.credential_store import CredentialStore


def open_file(run_dir, relative):
    return os.open(run_dir / relative, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)


def make_run(tmp_path, name, rows=()):
    run = tmp_path / name
    run.mkdir()
    (run / "run.json").write_text('{"status":"completed"}', encoding="utf-8")
    if rows:
        (run / "vulnerabilities.json").write_text(json.dumps([{
            "id": "synthetic-finding", "title": "Synthetic accounts", "metadata": {"credentials": rows},
        }]), encoding="utf-8")
    return run


def register(run, **fields):
    result = CredentialStore(run).record_credential(
        **{"host": "test.example", "username": "synthetic-user", "password": "synthetic-secret", **fields},
        agent_id="test-root", agent_name="Synthetic agent",
    )
    assert result["success"], result


def table_rows(snapshot):
    lines = snapshot["markdown"].splitlines()
    start = lines.index("| --- | --- | --- | --- | --- |") + 1
    result = []
    for line in lines[start:]:
        assert line.startswith("| ") and line.endswith(" |")
        cells = line[2:-2].split(" | ")
        assert len(cells) == 5
        assert all(cell.startswith("`") and cell.endswith("`") for cell in cells)
        result.append([json.loads(cell[1:-1]) for cell in cells])
    return result


def test_deduplicate_before_global_cap_with_validated_first_and_stable_order(tmp_path):
    rows = [
        {"host": f"host-{index:04d}", "username": "synthetic-user", "password": f"secret-{index:04d}"}
        for index in range(1005)
    ]
    rows[-1]["validation_status"] = "validated"
    first = make_run(tmp_path, "a", rows)
    second = make_run(tmp_path, "b", [*rows, {**rows[0], "password": "different-secret"}])
    result = snapshots.build_snapshot("project-test", [second, first, first], open_file)
    metadata = result["metadata"]
    assert metadata["total"] == 1006
    assert metadata["included"] == 1000
    assert metadata["omitted"] == 6
    assert metadata["run_count"] == metadata["contributing_run_count"] == 2
    assert metadata["limit"] == 1000
    assert metadata["source_status"] == "available"
    assert metadata["warnings"] == []
    assert datetime.fromisoformat(metadata["generated_at"]).utcoffset().total_seconds() == 0
    parsed = table_rows(result)
    assert len(parsed) == 1000
    assert parsed[0] == ["host-1004", "synthetic-user", "password", "secret-1004", "validated"]
    assert parsed[1][3] == "different-secret" and parsed[2][3] == "secret-0000"
    assert parsed[-1][0] == "host-0997"
    reordered = snapshots.build_snapshot("project-test", [first, second], open_file)
    assert table_rows(reordered) == parsed


def test_values_round_trip_without_markdown_damage_or_token_truncation(tmp_path):
    run = make_run(tmp_path, "special")
    host = ' https://synthetic.example/a|`<> &\n\\ " '
    username = ' user|`\r\n\t"\\名字\u2028\u2029 '
    password = '  ||`**&lt;<script>"\\\r\n\t密碼  '
    register(run, host=host, username=username, password=password)
    register(run, host="empty", username="blank-password", password="")
    register(run, host="username", username="account-only", password=None, secret_type="username")
    register(run, host="hash", password=None, hash="0123456789abcdef" * 4, secret_type="hash")
    register(run, host="token", secret_type="token", password="synthetic-token|\n`value")
    long_secret = "aB3!" * 8192
    register(run, host="long", password=long_secret, secret_type="private_key")
    result = snapshots.build_snapshot("project|`\n", [run], open_file)
    rows = {row[0]: row for row in table_rows(result)}
    assert rows[host] == [host, username, "password", password, "unverified"]
    assert rows["empty"][2:4] == ["password", ""]
    assert rows["username"][2:4] == ["username", ""]
    assert rows["hash"][2:4] == ["hash", "0123456789abcdef" * 4]
    assert rows["token"][2:4] == ["token", "synthetic-token|\n`value"]
    assert rows["long"][2:4] == ["private_key", long_secret]
    assert "\\u007c" in result["markdown"] and "\\u0060" in result["markdown"]
    assert result["metadata"]["included"] == result["metadata"]["total"] == 6
    assert result["metadata"]["sha256"] == hashlib.sha256(result["markdown"].encode()).hexdigest()


def test_exact_identity_keeps_distinct_secrets_types_and_whitespace(tmp_path):
    first = make_run(tmp_path, "first")
    second = make_run(tmp_path, "second")
    register(first)
    register(second)
    for fields in (
        {"password": "other-secret"}, {"password": " synthetic-secret"},
        {"secret_type": "api_key"}, {"host": "TEST.example"}, {"username": "Synthetic-user"},
    ):
        register(second, **fields)
    result = snapshots.build_snapshot("project-test", [first, second], open_file)
    assert result["metadata"]["total"] == 6
    assert len(table_rows(result)) == 6


def test_conflicting_validation_remains_unknown_and_sources_are_read_only(tmp_path):
    first = make_run(tmp_path, "first")
    second = make_run(tmp_path, "second")
    register(first, validation_status="validated", validation_evidence="Synthetic successful authentication")
    register(second, validation_status="failed", validation_evidence="Synthetic rejected authentication")
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = snapshots.build_snapshot("project-test", [first, second], open_file)
    after = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after
    assert table_rows(result)[0][-1] == "unknown"
    assert result["metadata"]["total"] == 1


def test_partial_sources_preserve_readable_rows_and_warnings(tmp_path):
    run = make_run(tmp_path, "partial")
    register(run)
    (run / "vulnerabilities.json").write_text("{broken", encoding="utf-8")
    result = snapshots.build_snapshot("project-test", [run], open_file)
    assert result["metadata"]["source_status"] == "partial"
    assert "vulnerabilities_unreadable" in result["metadata"]["warnings"]
    assert "vulnerabilities_unreadable" in result["markdown"]
    assert len(table_rows(result)) == 1


def test_missing_and_unreadable_sources_are_distinguished(tmp_path):
    missing = snapshots.build_snapshot("project-test", [], open_file)
    assert missing["metadata"]["source_status"] == "missing"
    assert missing["metadata"]["total"] == 0 and table_rows(missing) == []
    run = tmp_path / "unreadable"
    run.mkdir()
    (run / "run.json").write_text("{broken", encoding="utf-8")
    unreadable = snapshots.build_snapshot("project-test", [run], open_file)
    assert unreadable["metadata"]["source_status"] == "unreadable"
    assert unreadable["metadata"]["warnings"] == ["run_unreadable"]
    assert table_rows(unreadable) == []


def test_materialize_minimal_metadata_is_private_exclusive_and_loadable(tmp_path):
    snapshot = snapshots.build_snapshot("project-test", [], open_file)
    snapshot["metadata"] = {key: snapshot["metadata"][key] for key in ("sha256", "snapshot_file")}
    metadata = snapshots.materialize(tmp_path, snapshot)
    path = tmp_path / snapshots.SNAPSHOT
    assert path.read_text() == snapshot["markdown"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert metadata == snapshot["metadata"]
    assert snapshots.load_snapshot(tmp_path, metadata) == snapshot
    with pytest.raises(ValueError):
        snapshots.materialize(tmp_path, snapshot)
    assert path.read_text() == snapshot["markdown"]


@pytest.mark.parametrize("metadata", [
    {"sha256": "0" * 64}, {"snapshot_file": "../escape.md"}, {"included": True},
    {"included": 1001}, {"total": 1}, {"limit": 1001}, {"source_status": []},
    {"warnings": "not-a-list"}, {"warnings": [12]}, {"generated_at": "2026-09-27T01:00:00"},
    {"project_id": ""}, {"run_count": 0, "contributing_run_count": 1},
])
def test_invalid_metadata_rejected_before_creating_file(tmp_path, metadata):
    snapshot = snapshots.build_snapshot("project-test", [], open_file)
    snapshot["metadata"].update(metadata)
    with pytest.raises(ValueError):
        snapshots.materialize(tmp_path, snapshot)
    assert not (tmp_path / snapshots.SNAPSHOT).exists()


def test_materialize_and_load_refuse_symlinks_and_load_refuses_hardlinks(tmp_path):
    snapshot = snapshots.build_snapshot("project-test", [], open_file)
    destination = tmp_path / "destination"
    destination.mkdir()
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    path = destination / snapshots.SNAPSHOT
    path.symlink_to(target)
    for operation in (
        lambda: snapshots.materialize(destination, snapshot),
        lambda: snapshots.load_snapshot(destination, snapshot["metadata"]),
    ):
        with pytest.raises(ValueError):
            operation()
    assert target.read_text() == "unchanged"
    path.unlink()
    snapshots.materialize(destination, snapshot)
    alias = tmp_path / "alias"
    alias.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError):
        snapshots.load_snapshot(alias, snapshot["metadata"])
    with pytest.raises(ValueError):
        snapshots.materialize(alias, snapshot)
    os.link(path, tmp_path / "hardlink")
    with pytest.raises(ValueError):
        snapshots.load_snapshot(destination, snapshot["metadata"])


def test_load_detects_changed_contents_and_invalid_utf8(tmp_path):
    snapshot = snapshots.build_snapshot("project-test", [], open_file)
    metadata = snapshots.materialize(tmp_path, snapshot)
    path = tmp_path / snapshots.SNAPSHOT
    path.write_text(snapshot["markdown"] + "tampered", encoding="utf-8")
    with pytest.raises(ValueError):
        snapshots.load_snapshot(tmp_path, metadata)
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError):
        snapshots.load_snapshot(tmp_path, metadata)


def test_load_refuses_nonregular_file_without_blocking(tmp_path):
    snapshot = snapshots.build_snapshot("project-test", [], open_file)
    os.mkfifo(tmp_path / snapshots.SNAPSHOT)
    with pytest.raises(ValueError):
        snapshots.load_snapshot(tmp_path, snapshot["metadata"])
