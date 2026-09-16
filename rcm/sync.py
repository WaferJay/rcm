"""Active source-tree synchronization for proxy targets."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import posixpath
import shlex
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import ProxyTargetSpec, SyncMappingSpec, SyncSpec
from .http_sync import (
    SYNC_FILE_PREFIX,
    SYNC_MANIFEST_MEMBER,
    SYNC_MANIFEST_SCHEMA,
    matches_sync_exclude,
)
from .workspace import Workspace


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
    """Run locked, ordered one-way sync operations for a proxy target."""

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

    @property
    def uses_http(self) -> bool:
        return self.target.transport in {"http", "sse"} and self.target.ssh is None

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

    def _destination(
        self, mapping: SyncMappingSpec, workspace: Workspace | None = None
    ) -> str:
        if mapping.destination is None:
            raise SyncError(
                f"sync destination is not configured for target {self.target.name!r}"
            )
        destination = mapping.destination.rstrip("/") or "/"
        if workspace is not None:
            if mapping.workspace_destination is not None:
                destination = mapping.workspace_destination
            destination = self._workspace_destination(destination, workspace)
        if self.target.ssh is not None:
            # Permit an explicit rsync host in the destination, otherwise use
            # the SSH target's configured host and keep the YAML concise.
            if ":" not in destination.split("/", 1)[0]:
                return f"{self.target.ssh.host}:{destination}"
        return destination

    def _http_destination(
        self, mapping: SyncMappingSpec, workspace: Workspace | None
    ) -> str:
        if mapping.destination is None:
            raise SyncError(
                f"sync destination is not configured for target {self.target.name!r}"
            )
        destination = (
            mapping.workspace_destination
            if workspace is not None and mapping.workspace_destination is not None
            else mapping.destination
        )
        first = destination.split("/", 1)[0]
        normalized = posixpath.normpath(destination)
        if (
            destination.startswith("/")
            or ":" in first
            or "\\" in destination
            or normalized == ".."
            or normalized.startswith("../")
        ):
            raise SyncError(
                "HTTP sync.destination must be a relative POSIX path"
            )
        return normalized

    @staticmethod
    def _workspace_destination(destination: str, workspace: Workspace) -> str:
        """Resolve one mapping below an isolated command's base directory."""
        first = destination.split("/", 1)[0]
        if destination.startswith("/") or ":" in first or "\\" in destination:
            raise SyncError(
                "isolated sync.destination must be a relative POSIX path"
            )
        normalized = posixpath.normpath(destination)
        if normalized == ".":
            suffix = ""
        elif normalized == ".." or normalized.startswith("../"):
            raise SyncError(
                "isolated sync.destination must not escape the workspace"
            )
        else:
            suffix = f"/{normalized}"
        return f"{workspace.base_dir.as_posix().rstrip('/')}/{workspace.scope_id}{suffix}"

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

    def _command(
        self,
        mapping: SyncMappingSpec,
        source: Path,
        workspace: Workspace | None = None,
    ) -> list[str]:
        command = ["rsync", "-a", "--compress"]
        destination = self._destination(mapping, workspace)
        destination_host, destination_path = _split_remote_destination(destination)
        if destination_host is not None:
            # Unlike a local transfer, the remote rsync receiver only creates
            # the final destination component.  Prepare the complete path so
            # isolated destinations such as <base>/<scope>/<mapping> also work.
            remote_command = f"mkdir -p -- {shlex.quote(destination_path)} && rsync"
            command.append(f"--rsync-path={remote_command}")
        for pattern in self._protected_patterns(mapping, source):
            command.extend(("--exclude", pattern))
        if mapping.delete:
            command.append("--delete")
        command.extend(
            (
                "--",
                _trailing_slash(str(source)),
                _trailing_slash(destination),
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
            if self.uses_http:
                self._http_destination(mapping, None)

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

    @staticmethod
    def _matches_exclude(relative: str, pattern: str, *, directory: bool) -> bool:
        return matches_sync_exclude(relative, pattern, directory=directory)

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _http_manifest(
        self,
        mapping: SyncMappingSpec,
        source: Path,
        workspace: Workspace | None,
    ) -> tuple[dict[str, object], dict[str, Path]]:
        patterns = self._protected_patterns(mapping, source)
        entries: list[dict[str, object]] = []
        files: dict[str, Path] = {}

        def excluded(relative: str, *, directory: bool) -> bool:
            return any(
                self._matches_exclude(relative, pattern, directory=directory)
                for pattern in patterns
            )

        for current, directory_names, file_names in os.walk(
            source, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            kept_directories: list[str] = []
            for name in sorted(directory_names):
                path = current_path / name
                relative = path.relative_to(source).as_posix()
                if excluded(relative, directory=True):
                    continue
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    entries.append(
                        {
                            "path": relative,
                            "kind": "symlink",
                            "mode": stat.S_IMODE(metadata.st_mode),
                            "target": os.readlink(path),
                        }
                    )
                else:
                    entries.append(
                        {
                            "path": relative,
                            "kind": "directory",
                            "mode": stat.S_IMODE(metadata.st_mode),
                        }
                    )
                    kept_directories.append(name)
            directory_names[:] = kept_directories
            for name in sorted(file_names):
                path = current_path / name
                relative = path.relative_to(source).as_posix()
                if excluded(relative, directory=False):
                    continue
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    entries.append(
                        {
                            "path": relative,
                            "kind": "symlink",
                            "mode": stat.S_IMODE(metadata.st_mode),
                            "target": os.readlink(path),
                        }
                    )
                elif stat.S_ISREG(metadata.st_mode):
                    entries.append(
                        {
                            "path": relative,
                            "kind": "file",
                            "mode": stat.S_IMODE(metadata.st_mode),
                            "size": metadata.st_size,
                            "sha256": self._file_sha256(path),
                        }
                    )
                    files[relative] = path

        payload: dict[str, object] = {
            "schema": SYNC_MANIFEST_SCHEMA,
            "destination": self._http_destination(mapping, workspace),
            "delete": mapping.delete,
            "excludes": patterns,
            "entries": entries,
        }
        if workspace is not None:
            payload["scope_id"] = workspace.scope_id
            payload["workspace_base"] = str(workspace.base_dir)
        return payload, files

    @staticmethod
    def _http_archive(
        payload: dict[str, object],
        files: dict[str, Path],
        needed: list[str],
    ) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz", compresslevel=6) as archive:
            manifest = json.dumps(
                payload, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            info = tarfile.TarInfo(SYNC_MANIFEST_MEMBER)
            info.size = len(manifest)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(manifest))
            for relative in needed:
                path = files.get(relative)
                if path is None:
                    raise SyncError(
                        f"HTTP sync server requested unknown file {relative!r}"
                    )
                info = tarfile.TarInfo(f"{SYNC_FILE_PREFIX}{relative}")
                info.size = path.stat().st_size
                info.mode = 0o600
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
        return output.getvalue()

    def _http_endpoint(self, route: str) -> str:
        if self.target.endpoint is None:
            raise SyncError(
                f"HTTP sync target {self.target.name!r} has no endpoint"
            )
        parsed = urlsplit(self.target.endpoint)
        path = parsed.path.rstrip("/")
        for suffix in ("/mcp", "/sse"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        return urlunsplit((parsed.scheme, parsed.netloc, f"{path}{route}", "", ""))

    def _http_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, spec in self.target.headers.items():
            if spec.value is not None:
                headers[name] = spec.value
            elif spec.env is not None:
                value = os.environ.get(spec.env)
                if not value:
                    raise SyncError(
                        f"environment variable {spec.env!r} for target "
                        f"{self.target.name!r} header {name!r} is missing or empty"
                    )
                headers[name] = value
        return headers

    async def _http_sync_mapping(
        self,
        mapping: SyncMappingSpec,
        source: Path,
        workspace: Workspace | None,
    ) -> None:
        try:
            payload, files = await asyncio.to_thread(
                self._http_manifest, mapping, source, workspace
            )
            async with httpx.AsyncClient(
                headers=self._http_headers(), timeout=httpx.Timeout(60.0)
            ) as client:
                response = await client.post(
                    self._http_endpoint("/sync/v1/plan"), json=payload
                )
                response.raise_for_status()
                try:
                    result = response.json()
                except ValueError as exc:
                    raise SyncError(
                        "HTTP sync server returned an invalid JSON plan"
                    ) from exc
                needed = result.get("needed") if isinstance(result, dict) else None
                if (
                    not isinstance(needed, list)
                    or any(not isinstance(value, str) for value in needed)
                ):
                    raise SyncError("HTTP sync server returned an invalid plan")
                archive = await asyncio.to_thread(
                    self._http_archive, payload, files, needed
                )
                response = await client.post(
                    self._http_endpoint("/sync/v1/apply"),
                    content=archive,
                    headers={"Content-Type": "application/gzip"},
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                error = exc.response.json().get("error")
            except (ValueError, AttributeError):
                error = None
            detail = error if isinstance(error, str) else exc.response.text.strip()
            suffix = f": {detail}" if detail else ""
            raise SyncError(
                f"HTTP sync for target {self.target.name!r} failed with status "
                f"{exc.response.status_code}{suffix}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SyncError(
                f"HTTP sync for target {self.target.name!r} failed: {exc}"
            ) from exc
        except OSError as exc:
            raise SyncError(
                f"HTTP sync for target {self.target.name!r} could not read its "
                f"source files: {exc}"
            ) from exc

    async def sync(self, workspace: Workspace | None = None) -> None:
        """Synchronize all mappings in order, serializing calls for this target."""
        async with self._lock:
            if not self.uses_http and shutil.which("rsync") is None:
                raise SyncError("rsync executable not found")
            resolved = [
                (index, mapping, self._source_path(mapping, index))
                for index, mapping in enumerate(self.spec.mappings, start=1)
            ]
            for index, mapping, source in resolved:
                if self.uses_http:
                    try:
                        await self._http_sync_mapping(mapping, source, workspace)
                    except SyncError as exc:
                        raise SyncError(
                            f"sync mapping {index} for target {self.target.name!r} "
                            f"failed: {exc}"
                        ) from exc
                    continue
                destination = self._destination(mapping, workspace)
                command = self._command(mapping, source, workspace)
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
