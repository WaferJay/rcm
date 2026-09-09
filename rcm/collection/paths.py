"""Safe path matching and normalization for collection rules."""

from __future__ import annotations

import fnmatch
import os
import stat
from pathlib import Path


class PosixGlobMatcher:
    def expand(
        self,
        cwd: Path,
        pattern: str,
        protected_paths: tuple[Path, ...],
    ) -> list[Path]:
        return _expand_pattern(cwd, pattern, protected_paths)


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
            # synthetic "." archive root.
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
