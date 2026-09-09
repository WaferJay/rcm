"""Config-driven collection of command output files into run artifacts."""

from __future__ import annotations

import asyncio
import fnmatch
import gzip
import hashlib
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .artifacts import sha256_file
from .config import CollectPathSpec, CollectSpec


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


class TarGzipArchiveWriter:
    """Write a deterministic-header, level-6 gzip-compressed PAX tar."""

    def write(
        self,
        destination: Path,
        roots: list[tuple[str, Path]],
        *,
        protected_members: tuple[str, ...],
    ) -> ArchiveResult:
        with destination.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                compresslevel=6,
                mtime=0,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                    dereference=False,
                ) as archive:
                    for relative, source in roots:
                        archive.add(
                            source,
                            arcname=relative,
                            recursive=True,
                            filter=lambda info: _sanitize_member(
                                info,
                                protected_members=protected_members,
                            ),
                        )
        return ArchiveResult(
            bytes=destination.stat().st_size,
            sha256=sha256_file(destination),
        )


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
    """Immutable pre-command state used by ``mode: changed`` rules."""

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


class PosixGlobMatcher:
    def expand(
        self,
        cwd: Path,
        pattern: str,
        protected_paths: tuple[Path, ...],
    ) -> list[Path]:
        return _expand_pattern(cwd, pattern, protected_paths)


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


class TarGzipCollector:
    """Create a safe, ownership-anonymized tar.gz artifact."""

    kind = "collect"

    def __init__(
        self,
        archive_writer: ArchiveWriter | None = None,
        path_matcher: PathMatcher | None = None,
        change_detector: ChangeDetector | None = None,
    ) -> None:
        self._archive_writer = archive_writer or TarGzipArchiveWriter()
        self._path_matcher = path_matcher or PosixGlobMatcher()
        self._change_detector = change_detector or MetadataSha256ChangeDetector()

    @staticmethod
    def _availability_issue(
        source: Path,
        cwd: Path,
        protected_paths: tuple[Path, ...],
    ) -> str | None:
        """Compatibility wrapper around the filesystem policy helper."""
        return _availability_issue(source, cwd, protected_paths)

    async def prepare(
        self,
        spec: CollectSpec,
        *,
        cwd: Path,
        protected_paths: tuple[Path, ...],
    ) -> CollectionPreparation:
        try:
            return await asyncio.to_thread(
                self._prepare_sync,
                spec,
                cwd=cwd,
                protected_paths=protected_paths,
            )
        except (OSError, RuntimeError) as exc:
            rules = tuple(
                RulePreparation(
                    warnings=(
                        _warning(
                            code="collect_snapshot_failed",
                            path=item.path,
                            required=item.required,
                            message=f"collect baseline could not be created: {exc}",
                        ),
                    ),
                    scan_failed=True,
                )
                if _effective_mode(spec, item) == "changed"
                else RulePreparation()
                for item in spec.paths
            )
            return CollectionPreparation(rules=rules)

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
    ) -> CollectionOutcome:
        if prepared is None:
            prepared = await self.prepare(
                spec,
                cwd=cwd,
                protected_paths=protected_paths,
            )
        return await asyncio.to_thread(
            self._collect_sync,
            spec,
            cwd=cwd,
            destination=destination,
            protected_paths=protected_paths,
            prepared=prepared,
            command_started=command_started,
            returncode=returncode,
            timed_out=timed_out,
        )

    def _prepare_sync(
        self,
        spec: CollectSpec,
        *,
        cwd: Path,
        protected_paths: tuple[Path, ...],
    ) -> CollectionPreparation:
        cwd, protected = _normalize_roots(cwd, protected_paths)
        rules: list[RulePreparation] = []

        for item in spec.paths:
            if _effective_mode(spec, item) != "changed":
                rules.append(RulePreparation())
                continue

            snapshots: list[tuple[str, EntitySnapshot]] = []
            unknown: list[str] = []
            warnings: list[Warning] = []
            try:
                matches = self._path_matcher.expand(cwd, item.path, protected)
            except OSError as exc:
                warnings.append(
                    _warning(
                        code="collect_snapshot_failed",
                        path=item.path,
                        required=item.required,
                        message=f"collect baseline could not be scanned: {exc}",
                    )
                )
                rules.append(
                    RulePreparation(
                        warnings=tuple(warnings),
                        scan_failed=True,
                    )
                )
                continue

            for source in matches:
                relative = source.relative_to(cwd).as_posix()
                issue = self._availability_issue(source, cwd, protected)
                if issue is not None:
                    # A missing or inaccessible baseline can legitimately become a
                    # new artifact. Remember it as unknown instead of failing the run.
                    unknown.append(relative)
                    warnings.append(
                        _warning(
                            code="collect_snapshot_failed",
                            path=relative,
                            required=item.required,
                            message=(
                                "collect baseline could not be captured: "
                                f"{_warning_message(issue, item.required)}"
                            ),
                        )
                    )
                    continue
                try:
                    snapshot = self._change_detector.snapshot(
                        source,
                        protected,
                    )
                except (OSError, RuntimeError) as exc:
                    unknown.append(relative)
                    warnings.append(
                        _warning(
                            code="collect_snapshot_failed",
                            path=relative,
                            required=item.required,
                            message=f"collect baseline could not be read: {exc}",
                        )
                    )
                    continue
                snapshots.append((relative, snapshot))

            rules.append(
                RulePreparation(
                    snapshots=tuple(snapshots),
                    unknown_paths=tuple(unknown),
                    warnings=tuple(warnings),
                )
            )

        return CollectionPreparation(rules=tuple(rules))

    def _collect_sync(
        self,
        spec: CollectSpec,
        *,
        cwd: Path,
        destination: Path,
        protected_paths: tuple[Path, ...],
        prepared: CollectionPreparation,
        command_started: bool,
        returncode: int,
        timed_out: bool,
    ) -> CollectionOutcome:
        cwd, protected = _normalize_roots(cwd, protected_paths)
        warnings: list[Warning] = []
        selected: dict[str, Path] = {}
        required_unavailable = False

        for index, item in enumerate(spec.paths):
            if not _rule_applies(
                spec,
                item,
                command_started=command_started,
                returncode=returncode,
                timed_out=timed_out,
            ):
                continue

            baseline = (
                prepared.rules[index]
                if index < len(prepared.rules)
                else RulePreparation(scan_failed=True)
            )
            warnings.extend(baseline.warnings)
            if baseline.scan_failed:
                required_unavailable = required_unavailable or item.required
                continue

            try:
                matches = self._path_matcher.expand(cwd, item.path, protected)
            except OSError as exc:
                warnings.append(
                    _warning(
                        code="collect_path_unreadable",
                        path=item.path,
                        required=item.required,
                        message=f"collect pattern could not be scanned: {exc}",
                    )
                )
                required_unavailable = required_unavailable or item.required
                continue

            available: list[tuple[str, Path]] = []
            for source in matches:
                relative = source.relative_to(cwd).as_posix()
                issue = self._availability_issue(source, cwd, protected)
                if issue is not None:
                    warnings.append(
                        _warning(
                            code=issue,
                            path=relative,
                            required=item.required,
                            message=_warning_message(issue, item.required),
                        )
                    )
                    continue
                available.append((relative, source))

            before = dict(baseline.snapshots)
            if _effective_mode(spec, item) == "changed":
                after_names = {relative for relative, _ in available}
                for deleted in sorted(set(before) - after_names):
                    warnings.append(
                        _warning(
                            code="collect_path_deleted",
                            path=deleted,
                            required=item.required,
                            message="collect path was deleted while the command ran",
                        )
                    )

            if not available:
                code = (
                    "collect_no_matches"
                    if _has_magic(item.path)
                    else "collect_path_missing"
                )
                warnings.append(
                    _warning(
                        code=code,
                        path=item.path,
                        required=item.required,
                        message=_warning_message(code, item.required),
                    )
                )
                required_unavailable = required_unavailable or item.required
                continue

            if _effective_mode(spec, item) == "always":
                selected.update(available)
                continue

            unknown = set(baseline.unknown_paths)
            for relative, source in available:
                if relative in unknown:
                    # The pre-command contents were unreadable, so calling this
                    # changed would risk publishing a stale artifact.
                    required_unavailable = required_unavailable or item.required
                    continue
                previous = before.get(relative)
                if previous is None:
                    selected[relative] = source
                    continue
                try:
                    changed, deleted_members = self._change_detector.changed(
                        source,
                        previous,
                        protected,
                    )
                except (OSError, RuntimeError) as exc:
                    warnings.append(
                        _warning(
                            code="collect_snapshot_failed",
                            path=relative,
                            required=item.required,
                            message=f"collect path could not be compared: {exc}",
                        )
                    )
                    required_unavailable = required_unavailable or item.required
                    continue
                for deleted in deleted_members:
                    deleted_path = (
                        relative
                        if deleted == "."
                        else f"{relative}/{deleted}"
                    )
                    warnings.append(
                        _warning(
                            code="collect_path_deleted",
                            path=deleted_path,
                            required=item.required,
                            message="collect path was deleted while the command ran",
                        )
                    )
                if changed:
                    selected[relative] = source

        warnings = _deduplicate_warnings(warnings)
        if required_unavailable or not selected:
            return CollectionOutcome(published=False, warnings=tuple(warnings))

        roots = _minimal_roots(selected)
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            _remove_temporary(temporary)
            protected_members = _protected_members(cwd, protected)
            archive = self._archive_writer.write(
                temporary,
                roots,
                protected_members=protected_members,
            )
            temporary.replace(destination)
            return CollectionOutcome(
                published=True,
                bytes=archive.bytes,
                sha256=archive.sha256,
                warnings=tuple(warnings),
            )
        except Exception as exc:
            _remove_temporary(temporary)
            warnings.append(
                _warning(
                    code="collect_archive_failed",
                    path="",
                    required=True,
                    message=f"collect archive could not be created: {exc}",
                )
            )
            return CollectionOutcome(
                published=False,
                warnings=tuple(_deduplicate_warnings(warnings)),
            )


def _effective_mode(spec: CollectSpec, item: CollectPathSpec) -> str:
    return item.mode or spec.mode


def _effective_on_exit(spec: CollectSpec, item: CollectPathSpec) -> str:
    return item.on_exit or spec.on_exit


def _rule_applies(
    spec: CollectSpec,
    item: CollectPathSpec,
    *,
    command_started: bool,
    returncode: int,
    timed_out: bool,
) -> bool:
    if not command_started:
        return False
    if _effective_on_exit(spec, item) == "always":
        return True
    return returncode == 0 and not timed_out


def _normalize_roots(
    cwd: Path, protected_paths: tuple[Path, ...]
) -> tuple[Path, tuple[Path, ...]]:
    return (
        cwd.expanduser().resolve(),
        tuple(path.expanduser().resolve() for path in protected_paths),
    )


def _has_magic(pattern: str) -> bool:
    return any(character in pattern for character in "*?[")


def _expand_pattern(
    cwd: Path,
    pattern: str,
    protected_paths: tuple[Path, ...] = (),
) -> list[Path]:
    """Expand a validated POSIX glob without traversing symlink directories."""
    if not _has_magic(pattern):
        source = cwd.joinpath(*pattern.split("/"))
        return [source] if os.path.lexists(source) else []

    parts = tuple(pattern.split("/"))
    found: dict[str, Path] = {}

    def scan(directory: Path) -> list[os.DirEntry[str]]:
        try:
            with os.scandir(directory) as entries:
                return sorted(entries, key=lambda entry: entry.name)
        except (FileNotFoundError, NotADirectoryError):
            return []

    def visible(name: str, part: str) -> bool:
        return not name.startswith(".") or part.startswith(".")

    def add(path: Path) -> None:
        if path == cwd:
            # A leading recursive wildcard selects cwd's contents, not a
            # synthetic `.` archive root.
            return
        relative = path.relative_to(cwd).as_posix()
        found[relative] = path

    def protected(path: Path) -> bool:
        return _is_protected_member(path, protected_paths)

    def terminal_recursive(directory: Path) -> None:
        for entry in scan(directory):
            if not visible(entry.name, "**"):
                continue
            child = directory / entry.name
            add(child)
            if entry.is_dir(follow_symlinks=False) and not protected(child):
                terminal_recursive(child)

    def match(directory: Path, index: int) -> None:
        if index == len(parts):
            if os.path.lexists(directory):
                add(directory)
            return

        part = parts[index]
        if part == "**":
            match(directory, index + 1)
            if index == len(parts) - 1:
                terminal_recursive(directory)
                return
            for entry in scan(directory):
                if not visible(entry.name, part):
                    continue
                child = directory / entry.name
                if entry.is_dir(follow_symlinks=False) and not protected(child):
                    match(child, index)
            return

        for entry in scan(directory):
            if not visible(entry.name, part):
                continue
            if not fnmatch.fnmatchcase(entry.name, part):
                continue
            child = directory / entry.name
            if index == len(parts) - 1:
                add(child)
            elif entry.is_dir(follow_symlinks=False) and not protected(child):
                match(child, index + 1)

    match(cwd, 0)
    return [found[name] for name in sorted(found)]


def _availability_issue(
    source: Path,
    cwd: Path,
    protected_paths: tuple[Path, ...],
) -> str | None:
    try:
        metadata = _metadata(source)
        resolved = source.resolve(strict=True)
    except FileNotFoundError:
        return "collect_path_missing"
    except PermissionError:
        return "collect_path_unreadable"
    except (OSError, RuntimeError):
        return "collect_path_unsafe"

    if not _is_within(resolved, cwd):
        return "collect_path_unsafe"
    if any(_is_within(resolved, protected) for protected in protected_paths):
        return "collect_path_protected"
    if metadata.kind == "other":
        return "collect_path_unsupported"

    access_mode = os.R_OK | (os.X_OK if metadata.kind == "directory" else 0)
    if not os.access(resolved, access_mode):
        return "collect_path_unreadable"
    return None


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


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_protected_member(path: Path, protected_paths: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return True
    return any(_is_within(resolved, protected) for protected in protected_paths)


def _protected_members(cwd: Path, paths: tuple[Path, ...]) -> tuple[str, ...]:
    members: list[str] = []
    for path in paths:
        try:
            relative = path.relative_to(cwd)
        except ValueError:
            continue
        if relative != Path("."):
            members.append(relative.as_posix())
    return tuple(sorted(members))


def _minimal_roots(selected: dict[str, Path]) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    directories: list[str] = []
    for relative in sorted(selected):
        if any(relative.startswith(f"{parent}/") for parent in directories):
            continue
        source = selected[relative]
        roots.append((relative, source))
        try:
            is_directory = stat.S_ISDIR(source.lstat().st_mode)
        except OSError:
            is_directory = False
        if is_directory:
            directories.append(relative)
    return roots


def _sanitize_member(
    info: tarfile.TarInfo,
    *,
    protected_members: tuple[str, ...],
) -> tarfile.TarInfo | None:
    name = info.name.removeprefix("./").rstrip("/")
    for protected in protected_members:
        if name == protected or name.startswith(f"{protected}/"):
            return None
    if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
        return None

    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.pax_headers = {
        key: value
        for key, value in info.pax_headers.items()
        if key.rsplit(".", 1)[-1].lower() not in {"uid", "gid", "uname", "gname"}
    }
    return info


def _warning(
    *, code: str, path: str, required: bool, message: str
) -> Warning:
    return {
        "code": code,
        "path": path,
        "required": required,
        "message": message,
    }


def _warning_message(code: str, required: bool) -> str:
    requirement = "required" if required else "optional"
    reasons = {
        "collect_no_matches": "matched no collectable paths",
        "collect_path_missing": "does not exist",
        "collect_path_unsafe": "resolves outside the command working directory",
        "collect_path_protected": "overlaps a protected RCM path",
        "collect_path_unreadable": "is not readable",
        "collect_path_unsupported": "has an unsupported file type",
    }
    return f"{requirement} collect path {reasons[code]}"


def _deduplicate_warnings(warnings: list[Warning]) -> list[Warning]:
    unique: list[Warning] = []
    seen: set[tuple[object, ...]] = set()
    for warning in warnings:
        key = (
            warning.get("code"),
            warning.get("path"),
            warning.get("required"),
            warning.get("message"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(warning)
    return unique


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Cleanup must not hide the collection error returned to the caller.
        pass


# Backwards-compatible class name for callers that injected the original collector.
TarCollector = TarGzipCollector
DEFAULT_COLLECTOR: ArtifactCollector = TarGzipCollector()
