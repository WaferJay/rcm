"""Tests for the RCM v2 run-result protocol."""

from __future__ import annotations

import hashlib

import pytest

from rcm.artifacts import ArtifactError, parse_run_result


def result() -> dict:
    empty_hash = hashlib.sha256(b"").hexdigest()
    return {
        "schema": "rcm.run-result/v2",
        "run_id": "safe-run_id",
        "returncode": 0,
        "timed_out": False,
        "duration_ms": 1,
        "stdout": {"uri": "file:///tmp/stdout", "bytes": 0, "sha256": empty_hash},
        "stderr": {"uri": "file:///tmp/stderr", "bytes": 0, "sha256": empty_hash},
    }


def test_parse_run_result_v2() -> None:
    parsed = parse_run_result(result())
    assert parsed.run_id == "safe-run_id"
    assert parsed.stdout.bytes == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "../escape"),
        ("returncode", True),
        ("timed_out", 0),
        ("duration_ms", -1),
    ],
)
def test_parse_run_result_rejects_invalid_top_level_values(field, value) -> None:
    data = result()
    data[field] = value
    with pytest.raises(ArtifactError):
        parse_run_result(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("uri", ""),
        ("bytes", -1),
        ("bytes", True),
        ("sha256", "not-a-digest"),
    ],
)
def test_parse_run_result_rejects_invalid_artifact_values(field, value) -> None:
    data = result()
    data["stdout"][field] = value
    with pytest.raises(ArtifactError):
        parse_run_result(data)
