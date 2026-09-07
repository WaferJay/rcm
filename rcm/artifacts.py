"""RCM v2 run-result protocol shared by command and proxy runtimes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RCM_RESULT_SCHEMA = "rcm.run-result/v2"
RCM_CAPABILITY = "rcm.artifacts"
RCM_PROTOCOL_VERSION = 2
RCM_EXPERIMENTAL_CAPABILITIES: dict[str, dict[str, Any]] = {
    RCM_CAPABILITY: {
        "versions": [RCM_PROTOCOL_VERSION],
        "uriSchemes": ["file", "http", "https"],
    }
}
RCM_CALL_META: dict[str, Any] = {
    "rcm": {"artifacts": {"version": RCM_PROTOCOL_VERSION}}
}

ARTIFACT_MODES = {"localize", "passthrough"}
DEFAULT_ARTIFACT_MODE = "localize"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ArtifactError(RuntimeError):
    """Raised when an RCM v2 run result or artifact is invalid."""


@dataclass(frozen=True)
class ArtifactDescriptor:
    uri: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class RunResult:
    run_id: str
    returncode: int
    timed_out: bool
    duration_ms: int
    stdout: ArtifactDescriptor
    stderr: ArtifactDescriptor


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_run_result(value: Any) -> RunResult:
    """Validate and parse a structured RCM v2 tool result."""
    if not isinstance(value, dict) or value.get("schema") != RCM_RESULT_SCHEMA:
        raise ArtifactError("result is not an RCM v2 run result")

    run_id = value.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ArtifactError("RCM run result has an invalid run_id")

    returncode = value.get("returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise ArtifactError("RCM run result has an invalid returncode")
    timed_out = value.get("timed_out")
    if not isinstance(timed_out, bool):
        raise ArtifactError("RCM run result has an invalid timed_out value")
    duration_ms = value.get("duration_ms")
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms < 0
    ):
        raise ArtifactError("RCM run result has an invalid duration_ms")

    return RunResult(
        run_id=run_id,
        returncode=returncode,
        timed_out=timed_out,
        duration_ms=duration_ms,
        stdout=_parse_descriptor(value.get("stdout"), "stdout"),
        stderr=_parse_descriptor(value.get("stderr"), "stderr"),
    )


def _parse_descriptor(value: Any, stream: str) -> ArtifactDescriptor:
    if not isinstance(value, dict):
        raise ArtifactError(f"RCM run result is missing {stream}")
    uri = value.get("uri")
    if not isinstance(uri, str) or not uri:
        raise ArtifactError(f"RCM run result has an invalid {stream}.uri")
    size = value.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ArtifactError(f"RCM run result has an invalid {stream}.bytes")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ArtifactError(f"RCM run result has an invalid {stream}.sha256")
    return ArtifactDescriptor(uri=uri, bytes=size, sha256=digest)


def public_run_result(
    *,
    run_id: str,
    returncode: int,
    timed_out: bool,
    duration_ms: int,
    stdout_uri: str,
    stdout_bytes: int,
    stdout_sha256: str,
    stderr_uri: str,
    stderr_bytes: int,
    stderr_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": RCM_RESULT_SCHEMA,
        "run_id": run_id,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout": {
            "uri": stdout_uri,
            "bytes": stdout_bytes,
            "sha256": stdout_sha256,
        },
        "stderr": {
            "uri": stderr_uri,
            "bytes": stderr_bytes,
            "sha256": stderr_sha256,
        },
    }
