"""Orchestration for safe command artifact collection."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import CollectSpec
from .archive import TarGzipArchiveWriter
from .models import (
    ArchiveWriter,
    ArtifactCollector,
    ChangeDetector,
    CollectionOutcome,
    CollectionPreparation,
    PathMatcher,
    RulePreparation,
    Warning,
)
from .paths import (
    PosixGlobMatcher,
    _minimal_roots,
    _normalize_roots,
    _protected_members,
)
from .policy import (
    _availability_issue,
    _deduplicate_warnings,
    _effective_mode,
    _remove_temporary,
    _rule_applies,
    _warning,
)
from .rules import _prepare_rule, _select_rule
from .snapshots import MetadataSha256ChangeDetector


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
        rules = tuple(
            _prepare_rule(
                item,
                cwd=cwd,
                protected_paths=protected,
                path_matcher=self._path_matcher,
                change_detector=self._change_detector,
            )
            if _effective_mode(spec, item) == "changed"
            else RulePreparation()
            for item in spec.paths
        )
        return CollectionPreparation(rules=rules)

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
            result = _select_rule(
                spec,
                item,
                baseline,
                cwd=cwd,
                protected_paths=protected,
                path_matcher=self._path_matcher,
                change_detector=self._change_detector,
            )
            selected.update(result.selected)
            warnings.extend(result.warnings)
            required_unavailable = (
                required_unavailable or result.required_unavailable
            )

        warnings = _deduplicate_warnings(warnings)
        if required_unavailable or not selected:
            return CollectionOutcome(published=False, warnings=tuple(warnings))
        return self._publish_archive(
            destination,
            selected,
            warnings,
            cwd=cwd,
            protected_paths=protected,
        )

    def _publish_archive(
        self,
        destination: Path,
        selected: dict[str, Path],
        warnings: list[Warning],
        *,
        cwd: Path,
        protected_paths: tuple[Path, ...],
    ) -> CollectionOutcome:
        roots = _minimal_roots(selected)
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            _remove_temporary(temporary)
            archive = self._archive_writer.write(
                temporary,
                roots,
                protected_members=_protected_members(cwd, protected_paths),
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


# Backwards-compatible class name for callers that injected the original collector.
TarCollector = TarGzipCollector
DEFAULT_COLLECTOR: ArtifactCollector = TarGzipCollector()
