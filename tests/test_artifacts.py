"""Tests for the RCM v2 run-result protocol."""

from __future__ import annotations

import hashlib

import pytest

from rcm.artifacts import (
    ARTIFACT_KINDS,
    REQUIRED_ARTIFACT_KINDS,
    ArtifactError,
    parse_run_result,
    public_run_result,
)


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


def test_parse_run_result_with_collect_and_warnings() -> None:
    data = result()
    data["collect"] = {
        "uri": "https://example.test/runs/id/collect",
        "bytes": 3,
        "sha256": hashlib.sha256(b"tar").hexdigest(),
    }
    data["warnings"] = [
        {
            "code": "collect_path_missing",
            "path": "optional.txt",
            "required": False,
            "message": "optional collect path does not exist",
        }
    ]
    data["extension"] = {"kept": True}

    parsed = parse_run_result(data)

    assert parsed.collect is not None
    assert parsed.collect.bytes == 3
    assert parsed.warnings[0]["path"] == "optional.txt"
    assert parsed.extra_fields == {"extension": {"kept": True}}


def test_artifact_registry_drives_required_and_storage_properties() -> None:
    assert REQUIRED_ARTIFACT_KINDS == ("stdout", "stderr")
    assert ARTIFACT_KINDS["collect"].filename == "collect.tar.gz"
    assert ARTIFACT_KINDS["collect"].media_type == "application/gzip"
    assert ARTIFACT_KINDS["collect"].required is False


def test_public_run_result_keeps_legacy_stdout_stderr_arguments() -> None:
    digest = hashlib.sha256(b"").hexdigest()

    data = public_run_result(
        run_id="legacy",
        returncode=0,
        timed_out=False,
        duration_ms=1,
        stdout_uri="file:///tmp/stdout",
        stdout_bytes=0,
        stdout_sha256=digest,
        stderr_uri="file:///tmp/stderr",
        stderr_bytes=0,
        stderr_sha256=digest,
    )

    assert data == result() | {"run_id": "legacy"}


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
