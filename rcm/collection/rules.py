"""Preparation and selection of filesystem entities for collection rules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import CollectPathSpec, CollectSpec
from .models import (
    ChangeDetector,
    PathMatcher,
    RulePreparation,
    Warning,
)
from .paths import _has_magic
from .policy import (
    _availability_issue,
    _deleted_warnings,
    _effective_mode,
    _warning,
    _warning_message,
)


@dataclass(frozen=True)
class _MatchScan:
    available: tuple[tuple[str, Path], ...] = ()
    warnings: tuple[Warning, ...] = ()
    failed: bool = False


@dataclass(frozen=True)
class _RuleSelection:
    selected: tuple[tuple[str, Path], ...] = ()
    warnings: tuple[Warning, ...] = ()
    required_unavailable: bool = False


def _prepare_rule(
    item: CollectPathSpec,
    *,
    cwd: Path,
    protected_paths: tuple[Path, ...],
    path_matcher: PathMatcher,
    change_detector: ChangeDetector,
) -> RulePreparation:
    try:
        matches = path_matcher.expand(cwd, item.path, protected_paths)
    except OSError as exc:
        return RulePreparation(
            warnings=(
                _warning(
                    code="collect_snapshot_failed",
                    path=item.path,
                    required=item.required,
                    message=f"collect baseline could not be scanned: {exc}",
                ),
            ),
            scan_failed=True,
        )

    snapshots = []
    unknown = []
    warnings = []
    for source in matches:
        relative = source.relative_to(cwd).as_posix()
        issue = _availability_issue(source, cwd, protected_paths)
        if issue is not None:
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
            snapshot = change_detector.snapshot(source, protected_paths)
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

    return RulePreparation(
        snapshots=tuple(snapshots),
        unknown_paths=tuple(unknown),
        warnings=tuple(warnings),
    )


def _select_rule(
    spec: CollectSpec,
    item: CollectPathSpec,
    baseline: RulePreparation,
    *,
    cwd: Path,
    protected_paths: tuple[Path, ...],
    path_matcher: PathMatcher,
    change_detector: ChangeDetector,
) -> _RuleSelection:
    scan = _scan_available(
        item,
        cwd=cwd,
        protected_paths=protected_paths,
        path_matcher=path_matcher,
    )
    warnings = list(scan.warnings)
    if scan.failed:
        return _RuleSelection(
            warnings=tuple(warnings),
            required_unavailable=item.required,
        )

    before = dict(baseline.snapshots)
    if _effective_mode(spec, item) == "changed":
        after_names = {relative for relative, _ in scan.available}
        warnings.extend(
            _deleted_warnings(
                sorted(set(before) - after_names),
                required=item.required,
            )
        )

    if not scan.available:
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
        return _RuleSelection(
            warnings=tuple(warnings),
            required_unavailable=item.required,
        )

    if _effective_mode(spec, item) == "always":
        return _RuleSelection(
            selected=scan.available,
            warnings=tuple(warnings),
        )

    changed = _select_changed(
        item,
        scan.available,
        baseline,
        protected_paths=protected_paths,
        change_detector=change_detector,
    )
    warnings.extend(changed.warnings)
    return _RuleSelection(
        selected=changed.selected,
        warnings=tuple(warnings),
        required_unavailable=changed.required_unavailable,
    )


def _scan_available(
    item: CollectPathSpec,
    *,
    cwd: Path,
    protected_paths: tuple[Path, ...],
    path_matcher: PathMatcher,
) -> _MatchScan:
    try:
        matches = path_matcher.expand(cwd, item.path, protected_paths)
    except OSError as exc:
        return _MatchScan(
            warnings=(
                _warning(
                    code="collect_path_unreadable",
                    path=item.path,
                    required=item.required,
                    message=f"collect pattern could not be scanned: {exc}",
                ),
            ),
            failed=True,
        )

    available = []
    warnings = []
    for source in matches:
        relative = source.relative_to(cwd).as_posix()
        issue = _availability_issue(source, cwd, protected_paths)
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
    return _MatchScan(
        available=tuple(available),
        warnings=tuple(warnings),
    )


def _select_changed(
    item: CollectPathSpec,
    available: tuple[tuple[str, Path], ...],
    baseline: RulePreparation,
    *,
    protected_paths: tuple[Path, ...],
    change_detector: ChangeDetector,
) -> _RuleSelection:
    before = dict(baseline.snapshots)
    unknown = set(baseline.unknown_paths)
    selected = []
    warnings = []
    required_unavailable = False

    for relative, source in available:
        if relative in unknown:
            required_unavailable = required_unavailable or item.required
            continue
        previous = before.get(relative)
        if previous is None:
            selected.append((relative, source))
            continue
        try:
            changed, deleted_members = change_detector.changed(
                source,
                previous,
                protected_paths,
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
        deleted_paths = (
            relative if deleted == "." else f"{relative}/{deleted}"
            for deleted in deleted_members
        )
        warnings.extend(_deleted_warnings(deleted_paths, required=item.required))
        if changed:
            selected.append((relative, source))

    return _RuleSelection(
        selected=tuple(selected),
        warnings=tuple(warnings),
        required_unavailable=required_unavailable,
    )
