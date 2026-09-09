"""Filesystem snapshots and content-aware change detection."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .models import (
    EntitySnapshot,
    MemberMetadata,
    MemberSnapshot,
    SnapshotError,
)
from .paths import _is_protected_member


class MetadataSha256ChangeDetector:
    """Compare metadata first and SHA-256 contents when metadata is stable."""

    def snapshot(
        self,
        source: Path,
        protected_paths: tuple[Path, ...],
    ) -> EntitySnapshot:
        return _snapshot_entity(
            source,
            protected_paths=protected_paths,
            include_hashes=True,
        )

    def changed(
        self,
        source: Path,
        previous: EntitySnapshot,
        protected_paths: tuple[Path, ...],
    ) -> tuple[bool, tuple[str, ...]]:
        return _entity_changed(
            source,
            previous,
            protected_paths=protected_paths,
        )


def _metadata(path: Path) -> MemberMetadata:
    value = path.lstat()
    file_mode = value.st_mode
    if stat.S_ISREG(file_mode):
        kind = "file"
    elif stat.S_ISDIR(file_mode):
        kind = "directory"
    elif stat.S_ISLNK(file_mode):
        kind = "symlink"
    else:
        kind = "other"
    return MemberMetadata(
        kind=kind,
        mode=stat.S_IMODE(file_mode),
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        link_target=os.readlink(path) if kind == "symlink" else None,
    )


def _snapshot_entity(
    source: Path,
    *,
    protected_paths: tuple[Path, ...],
    include_hashes: bool,
) -> EntitySnapshot:
    members: list[MemberSnapshot] = []

    def visit(path: Path, relative: str) -> None:
        if relative != "." and _is_protected_member(path, protected_paths):
            return
        metadata = _metadata(path)
        if metadata.kind == "other":
            return
        digest = None
        if metadata.kind == "file" and include_hashes:
            digest = _stable_sha256(path, metadata)
        members.append(
            MemberSnapshot(path=relative, metadata=metadata, sha256=digest)
        )
        if metadata.kind != "directory":
            return
        try:
            with os.scandir(path) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            raise SnapshotError(str(exc)) from exc
        for entry in children:
            child_relative = (
                entry.name if relative == "." else f"{relative}/{entry.name}"
            )
            visit(path / entry.name, child_relative)

    visit(source, ".")
    return EntitySnapshot(members=tuple(members))


def _entity_changed(
    source: Path,
    previous: EntitySnapshot,
    *,
    protected_paths: tuple[Path, ...],
) -> tuple[bool, tuple[str, ...]]:
    current = _snapshot_entity(
        source,
        protected_paths=protected_paths,
        include_hashes=False,
    )
    before = {member.path: member for member in previous.members}
    after = {member.path: member for member in current.members}
    deleted = tuple(sorted(set(before) - set(after)))
    if set(before) != set(after):
        return True, deleted

    for path, old_member in before.items():
        if old_member.metadata != after[path].metadata:
            return True, deleted

    for path, old_member in before.items():
        if old_member.metadata.kind != "file":
            continue
        member_path = source if path == "." else source.joinpath(*path.split("/"))
        if _stable_sha256(member_path, after[path].metadata) != old_member.sha256:
            return True, deleted
    return False, deleted


def _stable_sha256(path: Path, expected: MemberMetadata) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise SnapshotError(str(exc)) from exc
    if _metadata(path) != expected:
        raise SnapshotError("file changed while it was being fingerprinted")
    return digest.hexdigest()
