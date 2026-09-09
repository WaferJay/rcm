"""Config-driven collection of command output files into run artifacts."""

from .archive import TarGzipArchiveWriter, _sanitize_member
from .collector import DEFAULT_COLLECTOR, TarCollector, TarGzipCollector
from .models import (
    ArchiveResult,
    ArchiveWriter,
    ArtifactCollector,
    ChangeDetector,
    CollectionOutcome,
    CollectionPreparation,
    EntitySnapshot,
    MemberMetadata,
    MemberSnapshot,
    PathMatcher,
    RulePreparation,
    SnapshotError,
    Warning,
)
from .paths import PosixGlobMatcher, _expand_pattern
from .snapshots import MetadataSha256ChangeDetector


# Preserve import, introspection, and pickle paths from the former single module.
_PUBLIC_TYPES = (
    ArchiveResult,
    ArchiveWriter,
    ArtifactCollector,
    ChangeDetector,
    CollectionOutcome,
    CollectionPreparation,
    EntitySnapshot,
    MemberMetadata,
    MemberSnapshot,
    MetadataSha256ChangeDetector,
    PathMatcher,
    PosixGlobMatcher,
    RulePreparation,
    SnapshotError,
    TarGzipArchiveWriter,
    TarGzipCollector,
)
for _public_type in _PUBLIC_TYPES:
    _public_type.__module__ = __name__
for _compat_function in (_expand_pattern, _sanitize_member):
    _compat_function.__module__ = __name__
del _compat_function, _public_type, _PUBLIC_TYPES


__all__ = [
    "ArchiveResult",
    "ArchiveWriter",
    "ArtifactCollector",
    "ChangeDetector",
    "CollectionOutcome",
    "CollectionPreparation",
    "DEFAULT_COLLECTOR",
    "EntitySnapshot",
    "MemberMetadata",
    "MemberSnapshot",
    "MetadataSha256ChangeDetector",
    "PathMatcher",
    "PosixGlobMatcher",
    "RulePreparation",
    "SnapshotError",
    "TarCollector",
    "TarGzipArchiveWriter",
    "TarGzipCollector",
    "Warning",
]
