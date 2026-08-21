"""Active source-tree synchronization for proxy targets."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from .config import ProxyTargetSpec, SyncSpec


class SyncError(RuntimeError):
    """Raised when a proxy target cannot be synchronized."""


class SyncRunner:
    """Run one locked, one-way rsync operation for a proxy target."""

    def __init__(self, target: ProxyTargetSpec, config_path: Path | None = None) -> None:
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
        self._lock = asyncio.Lock()

    def _source_path(self) -> Path:
        if self.spec.source is None:
            raise SyncError(f"sync source is not configured for target {self.target.name!r}")
        source = Path(self.spec.source).expanduser().resolve()
        if not source.exists():
            raise SyncError(f"sync source does not exist: {source}")
        if not source.is_dir():
            raise SyncError(f"sync source is not a directory: {source}")
        if not os.access(source, os.R_OK | os.X_OK):
            raise SyncError(f"sync source is not readable: {source}")
        return source

    def _destination(self) -> str:
        if self.spec.destination is None:
            raise SyncError(
                f"sync destination is not configured for target {self.target.name!r}"
            )
        destination = self.spec.destination.rstrip("/") or "/"
        if self.target.ssh is not None:
            # Permit an explicit rsync host in the destination, otherwise use
            # the SSH target's configured host and keep the YAML concise.
            if ":" not in destination.split("/", 1)[0]:
                return f"{self.target.ssh.host}:{destination}"
        return destination

    def _config_exclude(self, source: Path) -> str | None:
        if self.config_path is None:
            return None
        try:
            return self.config_path.relative_to(source).as_posix()
        except ValueError:
            return None

    def _command(self, source: Path) -> list[str]:
        command = ["rsync", "-a", "--compress"]
        patterns = list(self.spec.excludes)
        config_exclude = self._config_exclude(source)
        if config_exclude is not None and config_exclude not in patterns:
            patterns.append(config_exclude)
        for pattern in patterns:
            command.extend(("--exclude", pattern))
        if self.spec.delete:
            command.append("--delete")
        command.extend(("--", f"{source}/", f"{self._destination()}/"))
        return command

    async def sync(self) -> None:
        """Synchronize the target, serializing calls for this target."""
        async with self._lock:
            source = self._source_path()
            if shutil.which("rsync") is None:
                raise SyncError("rsync executable not found")
            command = self._command(source)
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                detail = stderr.decode(errors="replace").strip()
                if not detail:
                    detail = stdout.decode(errors="replace").strip()
                suffix = f": {detail}" if detail else ""
                raise SyncError(
                    f"sync for target {self.target.name!r} failed with exit code "
                    f"{proc.returncode}{suffix}"
                )
