"""Active source-tree synchronization for proxy targets."""

from __future__ import annotations

import asyncio
import os
import posixpath
import shutil
from pathlib import Path, PurePosixPath

from .config import ProxyTargetSpec, SyncMappingSpec, SyncSpec


class SyncError(RuntimeError):
    """Raised when a proxy target cannot be synchronized."""


def _split_remote_destination(destination: str) -> tuple[str | None, str]:
    """Split rsync's optional ``host:path`` form without mistaking path colons."""
    first = destination.split("/", 1)[0]
    if ":" not in first:
        return None, destination
    host, path = destination.split(":", 1)
    return host, path


def _trailing_slash(value: str) -> str:
    return value if value.endswith("/") else f"{value}/"


class SyncRunner:
    """Run locked, ordered, one-way rsync operations for a proxy target."""

    def __init__(
        self,
        target: ProxyTargetSpec,
        config_path: Path | None = None,
        remote_config_path: str | None = None,
    ) -> None:
        if target.sync is None:
            raise ValueError("a sync runner requires a configured sync section")
        self.target = target
        self.spec: SyncSpec = target.sync
        configured_path = config_path or os.environ.get("RCM_CONFIG")
        self.config_path = (
            Path(configured_path).expanduser().resolve()
            if configured_path is not None
            else None
        )
        self.remote_config_path = remote_config_path or (
            target.remote_config.path if target.remote_config is not None else None
        )
        self.runs_path = (
            Path(os.environ.get("RCM_RUNS_DIR", "./runs")).expanduser().resolve()
        )
        self._lock = asyncio.Lock()
        self._validate_destinations()

    def _source_path(self, mapping: SyncMappingSpec, index: int) -> Path:
        if mapping.source is None:
            raise SyncError(
                f"sync mapping {index} source is not configured for target "
                f"{self.target.name!r}"
            )
        source = Path(mapping.source).expanduser().resolve()
        if not source.exists():
            raise SyncError(f"sync mapping {index} source does not exist: {source}")
        if not source.is_dir():
            raise SyncError(f"sync mapping {index} source is not a directory: {source}")
        if not os.access(source, os.R_OK | os.X_OK):
            raise SyncError(f"sync mapping {index} source is not readable: {source}")
        try:
            source.relative_to(self.runs_path)
        except ValueError:
            pass
        else:
            raise SyncError(
                f"sync mapping {index} source is inside the protected runs "
                f"directory: {source}"
            )
        return source

    def _destination(self, mapping: SyncMappingSpec) -> str:
        if mapping.destination is None:
            raise SyncError(
                f"sync destination is not configured for target {self.target.name!r}"
            )
        destination = mapping.destination.rstrip("/") or "/"
        if self.target.ssh is not None:
            # Permit an explicit rsync host in the destination, otherwise use
            # the SSH target's configured host and keep the YAML concise.
            if ":" not in destination.split("/", 1)[0]:
                return f"{self.target.ssh.host}:{destination}"
        return destination

    @staticmethod
    def _local_exclude(
        path: Path | None,
        source: Path,
        *,
        directory: bool,
    ) -> str | None:
        if path is None:
            return None
        try:
            relative = path.relative_to(source)
        except ValueError:
            return None
        if relative == Path("."):
            return "/" if directory else None
        suffix = "/" if directory else ""
        return f"/{relative.as_posix()}{suffix}"

    def _remote_config_exclude(self, mapping: SyncMappingSpec) -> str | None:
        if self.remote_config_path is None or self.target.ssh is None:
            return None
        destination_host, destination_path = _split_remote_destination(
            self._destination(mapping)
        )
        if (
            destination_host != self.target.ssh.host
            or not destination_path.startswith("/")
        ):
            return None
        try:
            relative = PurePosixPath(self.remote_config_path).relative_to(
                PurePosixPath(destination_path)
            )
        except ValueError:
            return None
        if relative == PurePosixPath("."):
            return None
        return f"/{relative.as_posix()}"

    def _protected_patterns(
        self,
        mapping: SyncMappingSpec,
        source: Path,
    ) -> list[str]:
        patterns = list(mapping.excludes)
        protected = [
            self._local_exclude(self.config_path, source, directory=False),
            self._local_exclude(self.runs_path, source, directory=True),
            self._remote_config_exclude(mapping),
            "/runs/",
        ]
        for pattern in protected:
            if pattern is not None and pattern not in patterns:
                patterns.append(pattern)
        return patterns

    def _command(self, mapping: SyncMappingSpec, source: Path) -> list[str]:
        command = ["rsync", "-a", "--compress"]
        for pattern in self._protected_patterns(mapping, source):
            command.extend(("--exclude", pattern))
        if mapping.delete:
            command.append("--delete")
        command.extend(
            (
                "--",
                _trailing_slash(str(source)),
                _trailing_slash(self._destination(mapping)),
            )
        )
        return command

    def _destination_identity(
        self,
        mapping: SyncMappingSpec,
    ) -> tuple[str | None, bool, tuple[str, ...]]:
        destination = self._destination(mapping)
        host, path = _split_remote_destination(destination)
        if host is None:
            local = Path(path).expanduser().resolve()
            return None, True, local.parts
        normalized = posixpath.normpath(path)
        absolute = normalized.startswith("/")
        return host, absolute, PurePosixPath(normalized).parts

    def _validate_destinations(self) -> None:
        mappings = self.spec.mappings
        if not mappings:
            raise SyncError(
                f"sync for target {self.target.name!r} requires at least one mapping"
            )
        for index, mapping in enumerate(mappings, start=1):
            if mapping.source is None or mapping.destination is None:
                raise SyncError(
                    f"sync mapping {index} for target {self.target.name!r} "
                    "requires source and destination"
                )

        for left_index, left in enumerate(mappings):
            left_host, left_absolute, left_parts = self._destination_identity(left)
            for right_index in range(left_index + 1, len(mappings)):
                right = mappings[right_index]
                if not (left.delete or right.delete):
                    continue
                right_host, right_absolute, right_parts = self._destination_identity(
                    right
                )
                if left_host != right_host or left_absolute != right_absolute:
                    continue
                shorter = min(len(left_parts), len(right_parts))
                if left_parts[:shorter] == right_parts[:shorter]:
                    raise SyncError(
                        f"sync mappings {left_index + 1} and {right_index + 1} for "
                        f"target {self.target.name!r} have overlapping destinations "
                        "while delete is enabled"
                    )

    async def sync(self) -> None:
        """Synchronize all mappings in order, serializing calls for this target."""
        async with self._lock:
            if shutil.which("rsync") is None:
                raise SyncError("rsync executable not found")
            resolved = [
                (index, mapping, self._source_path(mapping, index))
                for index, mapping in enumerate(self.spec.mappings, start=1)
            ]
            for index, mapping, source in resolved:
                destination = self._destination(mapping)
                command = self._command(mapping, source)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except OSError as exc:
                    raise SyncError(
                        f"sync mapping {index} for target {self.target.name!r} "
                        f"({source} -> {destination}) could not start: {exc}"
                    ) from exc
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    detail = stderr.decode(errors="replace").strip()
                    if not detail:
                        detail = stdout.decode(errors="replace").strip()
                    suffix = f": {detail}" if detail else ""
                    raise SyncError(
                        f"sync mapping {index} for target {self.target.name!r} "
                        f"({source} -> {destination}) failed with exit code "
                        f"{proc.returncode}{suffix}"
                    )
