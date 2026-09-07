"""Persist only a validated container/run binding; IO failures stay nonfatal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strixops.runtime import proxy_binding

CONTAINER_ID = "0123456789abcdef" * 4


def test_records_private_minimal_runtime_binding(tmp_path: Path) -> None:
    run_dir = tmp_path / "example-com_abcd"
    run_dir.mkdir()

    assert proxy_binding.record_caido_runtime(run_dir, CONTAINER_ID) is True

    path = run_dir / ".state" / "caido-runtime.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "container_id": CONTAINER_ID,
        "run_name": run_dir.name,
    }
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize(
    "container_id",
    [None, 123, "", "a" * 12, "a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + CONTAINER_ID],
)
def test_invalid_container_identifiers_are_not_written(tmp_path: Path, container_id: object) -> None:
    assert proxy_binding.record_caido_runtime(tmp_path, container_id) is False
    assert list(tmp_path.iterdir()) == []


def test_replaces_previous_binding_without_extra_fields(tmp_path: Path) -> None:
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    path = state_dir / "caido-runtime.json"
    path.write_text(
        json.dumps({"container_id": "f" * 64, "run_name": "old", "token": "obsolete-test-token"}),
        encoding="utf-8",
    )

    assert proxy_binding.record_caido_runtime(tmp_path, CONTAINER_ID) is True

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "container_id": CONTAINER_ID,
        "run_name": tmp_path.name,
    }
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(state_dir.iterdir()) == [path]


def test_io_error_is_nonfatal_and_does_not_log_sensitive_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    private_detail = "test-secret-that-must-not-be-logged"

    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError(private_detail)

    monkeypatch.setattr(Path, "mkdir", denied)

    assert proxy_binding.record_caido_runtime(tmp_path, CONTAINER_ID) is False

    assert list(tmp_path.iterdir()) == []
    assert any(record.levelname == "WARNING" for record in caplog.records)
    assert private_detail not in caplog.text
    assert CONTAINER_ID not in caplog.text


def test_failed_atomic_replace_preserves_previous_binding_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    destination = state_dir / "caido-runtime.json"
    previous = json.dumps({"container_id": "f" * 64, "run_name": tmp_path.name}).encode()
    destination.write_bytes(previous)

    def failed_replace(source: Path, target: Path) -> None:
        assert target == destination
        assert destination.read_bytes() == previous
        assert source.stat().st_mode & 0o777 == 0o600
        assert json.loads(source.read_text(encoding="utf-8"))["container_id"] == CONTAINER_ID
        raise OSError("replacement denied")

    monkeypatch.setattr(proxy_binding.os, "replace", failed_replace)

    assert proxy_binding.record_caido_runtime(tmp_path, CONTAINER_ID) is False
    assert destination.read_bytes() == previous
    assert list(state_dir.iterdir()) == [destination]
