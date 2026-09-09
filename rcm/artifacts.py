"""RCM v2 run-result protocol shared by command and proxy runtimes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

RCM_RESULT_SCHEMA = "rcm.run-result/v2"
RCM_CAPABILITY = "rcm.artifacts"
RCM_PROTOCOL_VERSION = 2
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
class ArtifactKind:
    name: str
    filename: str
    media_type: str
    required: bool = False
    supports_tail: bool = False


ARTIFACT_KINDS: Mapping[str, ArtifactKind] = MappingProxyType(
    {
        item.name: item
        for item in (
            ArtifactKind(
                name="stdout",
                filename="stdout.log",
                media_type="text/plain; charset=utf-8",
                required=True,
                supports_tail=True,
            ),
            ArtifactKind(
                name="stderr",
                filename="stderr.log",
                media_type="text/plain; charset=utf-8",
                required=True,
                supports_tail=True,
            ),
            ArtifactKind(
                name="collect",
                filename="collect.tar.gz",
                media_type="application/gzip",
            ),
        )
    }
)
REQUIRED_ARTIFACT_KINDS = tuple(
    name for name, kind in ARTIFACT_KINDS.items() if kind.required
)
RCM_EXPERIMENTAL_CAPABILITIES: dict[str, dict[str, Any]] = {
    RCM_CAPABILITY: {
        "versions": [RCM_PROTOCOL_VERSION],
        "uriSchemes": ["file", "http", "https"],
        "artifactKinds": list(ARTIFACT_KINDS),
    }
}


@dataclass(frozen=True)
class ArtifactDescriptor:
    uri: str
    bytes: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"uri": self.uri, "bytes": self.bytes, "sha256": self.sha256}


@dataclass(frozen=True)
class RunResult:
    run_id: str
    returncode: int
    timed_out: bool
    duration_ms: int
    artifacts: Mapping[str, ArtifactDescriptor]
    warnings: tuple[dict[str, Any], ...] = ()
    extra_fields: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def stdout(self) -> ArtifactDescriptor:
        return self.artifacts["stdout"]

    @property
    def stderr(self) -> ArtifactDescriptor:
        return self.artifacts["stderr"]

    @property
    def collect(self) -> ArtifactDescriptor | None:
        return self.artifacts.get("collect")


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

    artifacts: dict[str, ArtifactDescriptor] = {}
    for name in ARTIFACT_KINDS:
        descriptor = value.get(name)
        if descriptor is None and name not in REQUIRED_ARTIFACT_KINDS:
            continue
        artifacts[name] = _parse_descriptor(descriptor, name)

    warnings_raw = value.get("warnings", [])
    if not isinstance(warnings_raw, list):
        raise ArtifactError("RCM run result has invalid warnings")
    warnings: list[dict[str, Any]] = []
    for index, warning in enumerate(warnings_raw):
        if not isinstance(warning, dict):
            raise ArtifactError(f"RCM run result warning {index} is invalid")
        if not isinstance(warning.get("code"), str) or not warning["code"]:
            raise ArtifactError(f"RCM run result warning {index} has an invalid code")
        if not isinstance(warning.get("message"), str) or not warning["message"]:
            raise ArtifactError(
                f"RCM run result warning {index} has an invalid message"
            )
        warnings.append(dict(warning))

    core_fields = {
        "schema",
        "run_id",
        "returncode",
        "timed_out",
        "duration_ms",
        "warnings",
        *ARTIFACT_KINDS,
    }
    return RunResult(
        run_id=run_id,
        returncode=returncode,
        timed_out=timed_out,
        duration_ms=duration_ms,
        artifacts=MappingProxyType(artifacts),
        warnings=tuple(warnings),
        extra_fields=MappingProxyType(
            {key: item for key, item in value.items() if key not in core_fields}
        ),
    )


def _parse_descriptor(value: Any, artifact: str) -> ArtifactDescriptor:
    if not isinstance(value, dict):
        raise ArtifactError(f"RCM run result is missing {artifact}")
    uri = value.get("uri")
    if not isinstance(uri, str) or not uri:
        raise ArtifactError(f"RCM run result has an invalid {artifact}.uri")
    size = value.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ArtifactError(f"RCM run result has an invalid {artifact}.bytes")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ArtifactError(f"RCM run result has an invalid {artifact}.sha256")
    return ArtifactDescriptor(uri=uri, bytes=size, sha256=digest)


def public_run_result(
    *,
    run_id: str,
    returncode: int,
    timed_out: bool,
    duration_ms: int,
    artifacts: Mapping[str, ArtifactDescriptor] | None = None,
    warnings: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    extra_fields: Mapping[str, Any] | None = None,
    stdout_uri: str | None = None,
    stdout_bytes: int | None = None,
    stdout_sha256: str | None = None,
    stderr_uri: str | None = None,
    stderr_bytes: int | None = None,
    stderr_sha256: str | None = None,
) -> dict[str, Any]:
    legacy_values = (
        stdout_uri,
        stdout_bytes,
        stdout_sha256,
        stderr_uri,
        stderr_bytes,
        stderr_sha256,
    )
    if artifacts is None:
        if any(value is None for value in legacy_values):
            raise ArtifactError("run result requires stdout and stderr artifacts")
        assert stdout_uri is not None
        assert stdout_bytes is not None
        assert stdout_sha256 is not None
        assert stderr_uri is not None
        assert stderr_bytes is not None
        assert stderr_sha256 is not None
        artifacts = {
            "stdout": ArtifactDescriptor(
                uri=stdout_uri,
                bytes=stdout_bytes,
                sha256=stdout_sha256,
            ),
            "stderr": ArtifactDescriptor(
                uri=stderr_uri,
                bytes=stderr_bytes,
                sha256=stderr_sha256,
            ),
        }
    elif any(value is not None for value in legacy_values):
        raise ArtifactError(
            "artifacts cannot be combined with legacy stdout/stderr fields"
        )
    missing = set(REQUIRED_ARTIFACT_KINDS) - set(artifacts)
    if missing:
        raise ArtifactError(f"run result is missing artifacts: {sorted(missing)}")
    result: dict[str, Any] = {
        **dict(extra_fields or {}),
        "schema": RCM_RESULT_SCHEMA,
        "run_id": run_id,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
    }
    for name, descriptor in artifacts.items():
        if name not in ARTIFACT_KINDS:
            raise ArtifactError(f"unknown artifact kind {name!r}")
        result[name] = descriptor.as_dict()
    if warnings:
        result["warnings"] = [dict(warning) for warning in warnings]
    return result
