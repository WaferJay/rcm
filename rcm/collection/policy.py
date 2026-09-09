"""Collection rule policy, warnings, and path availability checks."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from ..config import CollectPathSpec, CollectSpec
from .models import Warning
from .paths import _is_within
from .snapshots import _metadata


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


def _deleted_warnings(
    paths: Iterable[str],
    *,
    required: bool,
) -> list[Warning]:
    return [
        _warning(
            code="collect_path_deleted",
            path=path,
            required=required,
            message="collect path was deleted while the command ran",
        )
        for path in paths
    ]


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
