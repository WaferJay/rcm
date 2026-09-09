"""Contracts and immutable state for command artifact collection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import CollectSpec


Warning = dict[str, object]


@dataclass(frozen=True)
class CollectionOutcome:
    """Result of an optional post-command artifact production step."""

    published: bool
    bytes: int = 0
    sha256: str = ""
    warnings: tuple[Warning, ...] = ()


@dataclass(frozen=True)
class ArchiveResult:
    bytes: int
    sha256: str


class ArchiveWriter(Protocol):
    """Pluggable serialization strategy for selected collection roots."""

    def write(
        self,
        destination: Path,
        roots: list[tuple[str, Path]],
        *,
        protected_members: tuple[str, ...],
    ) -> ArchiveResult: ...


@dataclass(frozen=True)
class MemberMetadata:
    kind: str
    mode: int
    size: int
    mtime_ns: int
    link_target: str | None = None


@dataclass(frozen=True)
class MemberSnapshot:
    path: str
    metadata: MemberMetadata
    sha256: str | None = None


@dataclass(frozen=True)
class EntitySnapshot:
    members: tuple[MemberSnapshot, ...]


@dataclass(frozen=True)
class RulePreparation:
    snapshots: tuple[tuple[str, EntitySnapshot], ...] = ()
    unknown_paths: tuple[str, ...] = ()
    warnings: tuple[Warning, ...] = ()
    scan_failed: bool = False


@dataclass(frozen=True)
class CollectionPreparation:
    """Immutable pre-command state used by changed-mode rules."""

    rules: tuple[RulePreparation, ...]


class ArtifactCollector(Protocol):
    """Extension point for producing an artifact around command execution."""

    kind: str

    async def prepare(
        self,
        spec: CollectSpec,
        *,
        cwd: Path,
        protected_paths: tuple[Path, ...],
    ) -> CollectionPreparation: ...

    async def collect(
        self,
        spec: CollectSpec,
        *,
        cwd: Path,
        destination: Path,
        protected_paths: tuple[Path, ...],
        prepared: CollectionPreparation | None = None,
        command_started: bool = True,
        returncode: int = 0,
        timed_out: bool = False,
    ) -> CollectionOutcome: ...


class SnapshotError(OSError):
    """Raised when a stable filesystem snapshot cannot be produced."""


class PathMatcher(Protocol):
    """Expand configured paths without crossing protected directories."""

    def expand(
        self,
        cwd: Path,
        pattern: str,
        protected_paths: tuple[Path, ...],
    ) -> list[Path]: ...


class ChangeDetector(Protocol):
    """Capture and compare one matched filesystem entity."""

    def snapshot(
        self,
        source: Path,
        protected_paths: tuple[Path, ...],
    ) -> EntitySnapshot: ...

    def changed(
        self,
        source: Path,
        previous: EntitySnapshot,
        protected_paths: tuple[Path, ...],
    ) -> tuple[bool, tuple[str, ...]]: ...
