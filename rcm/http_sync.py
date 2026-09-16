"""Authenticated HTTP file synchronization for RCM peers."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import io
import json
import os
import secrets
import shutil
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import Config
from .store import Store
from .workspace import SCOPE_ID_RE, workspace_base_dir


SYNC_MANIFEST_SCHEMA = "rcm.sync-manifest/v1"
SYNC_MANIFEST_MEMBER = ".rcm-sync-manifest.json"
SYNC_FILE_PREFIX = "files/"


class HttpSyncError(RuntimeError):
    """A client-visible HTTP synchronization error."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class _DestinationContext:
    """Resolved write and containment roots for one synchronization mapping."""

    root: Path
    allowed_root: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matches_sync_exclude(relative: str, pattern: str, *, directory: bool) -> bool:
    """Match the rsync-style glob subset accepted by proxy configuration."""
    pattern = pattern.lstrip("/")
    relative = relative.rstrip("/")
    if not relative:
        return False
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        if relative == prefix or relative.startswith(f"{prefix}/"):
            return True
    patterns = (pattern, pattern[3:]) if pattern.startswith("**/") else (pattern,)
    candidates = (relative, f"{relative}/") if directory else (relative,)
    return any(
        fnmatch.fnmatchcase(candidate, candidate_pattern)
        for candidate in candidates
        for candidate_pattern in patterns
    )


def _relative_path(value: Any, field: str, *, allow_dot: bool = False) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise HttpSyncError(f"{field} must be a non-empty relative POSIX path")
    if value == "." and allow_dot:
        return PurePosixPath(value)
    path = PurePosixPath(value)
    if path.is_absolute() or "\\" in value or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise HttpSyncError(f"{field} must be a safe relative POSIX path")
    return path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class HttpSyncService:
    """Validate and apply versioned synchronization requests."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        api_key: str | None,
    ) -> None:
        self.api_key = api_key
        self.default_root = Path(cfg.defaults.cwd or os.getcwd()).expanduser().resolve()
        self.workspace_roots = {self.default_root}
        for command in cfg.commands:
            isolate = command.isolate
            if isolate is not None and isolate.by != "none":
                self.workspace_roots.add(
                    workspace_base_dir(isolate, default_cwd=cfg.defaults.cwd)
                )
        self.protected_files = (
            {cfg.config_path.expanduser().resolve()} if cfg.config_path is not None else set()
        )
        self.protected_dirs = {store.runs_dir.expanduser().resolve()}

    def _authenticate(self, request: Request) -> None:
        if self.api_key is None:
            return
        value = request.headers.get("authorization", "")
        if not value.lower().startswith("bearer ") or not secrets.compare_digest(
            value[7:].strip(), self.api_key
        ):
            raise HttpSyncError("unauthorized", 401)

    def _destination_context(self, payload: dict[str, Any]) -> _DestinationContext:
        destination = _relative_path(
            payload.get("destination"), "destination", allow_dot=True
        )
        scope_id = payload.get("scope_id")
        workspace_base = payload.get("workspace_base")
        if scope_id is None and workspace_base is None:
            base = self.default_root
        elif (
            isinstance(scope_id, str)
            and SCOPE_ID_RE.fullmatch(scope_id)
            and isinstance(workspace_base, str)
        ):
            base = Path(workspace_base).expanduser().resolve()
            if base not in self.workspace_roots:
                raise HttpSyncError("workspace_base is not an allowed workspace root")
            base = base / scope_id
        else:
            raise HttpSyncError(
                "scope_id and workspace_base must be supplied together and be valid"
            )
        root = (base / destination.as_posix()).resolve()
        allowed = base.resolve()
        if not _inside(root, allowed):
            raise HttpSyncError("destination escapes its allowed synchronization root")
        if self._is_protected(root):
            raise HttpSyncError("destination is protected from synchronization")
        return _DestinationContext(root=root, allowed_root=allowed)

    def _is_protected(self, path: Path) -> bool:
        resolved = path.resolve(strict=False)
        if resolved in self.protected_files:
            return True
        return any(resolved == root or _inside(resolved, root) for root in self.protected_dirs)

    def _contains_protected(self, path: Path) -> bool:
        resolved = path.resolve(strict=False)
        protected = self.protected_files | self.protected_dirs
        return any(item == resolved or _inside(item, resolved) for item in protected)

    def _replace_existing(self, path: Path) -> None:
        if self._contains_protected(path):
            raise HttpSyncError(f"path {path.name!r} contains protected RCM data")
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    @staticmethod
    def _validate_symlink_target(
        name: str, target: str, destination: _DestinationContext
    ) -> None:
        """Reject relative link targets that climb above the workspace root."""
        target_path = PurePosixPath(target)
        if target_path.is_absolute():
            raise HttpSyncError(
                f"symlink {name!r} escapes the allowed synchronization root"
            )
        mapping_depth = len(destination.root.relative_to(destination.allowed_root).parts)
        parent_depth = len(PurePosixPath(name).parent.parts)
        depth = mapping_depth + parent_depth
        for part in target_path.parts:
            if part == "..":
                depth -= 1
            elif part not in {"", "."}:
                depth += 1
            if depth < 0:
                raise HttpSyncError(
                    f"symlink {name!r} escapes the allowed synchronization root"
                )

    def _entries(
        self, payload: dict[str, Any], destination: _DestinationContext
    ) -> list[dict[str, Any]]:
        if payload.get("schema") != SYNC_MANIFEST_SCHEMA:
            raise HttpSyncError("unsupported sync manifest schema")
        if not isinstance(payload.get("delete"), bool):
            raise HttpSyncError("delete must be a boolean")
        excludes = payload.get("excludes")
        if not isinstance(excludes, list) or any(
            not isinstance(pattern, str) or not pattern for pattern in excludes
        ):
            raise HttpSyncError("excludes must be a list of non-empty strings")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise HttpSyncError("entries must be a list")
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_entries):
            if not isinstance(raw, dict):
                raise HttpSyncError(f"entries[{index}] must be an object")
            relative = _relative_path(raw.get("path"), f"entries[{index}].path")
            name = relative.as_posix()
            if name in seen:
                raise HttpSyncError(f"duplicate manifest path: {name}")
            seen.add(name)
            kind = raw.get("kind")
            mode = raw.get("mode")
            if kind not in {"file", "directory", "symlink"}:
                raise HttpSyncError(f"entries[{index}].kind is invalid")
            if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
                raise HttpSyncError(f"entries[{index}].mode is invalid")
            entry = {"path": name, "kind": kind, "mode": mode}
            if kind == "file":
                size = raw.get("size")
                digest = raw.get("sha256")
                if (
                    isinstance(size, bool)
                    or not isinstance(size, int)
                    or size < 0
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                ):
                    raise HttpSyncError(f"entries[{index}] has invalid file metadata")
                entry.update(size=size, sha256=digest)
            elif kind == "symlink":
                target = raw.get("target")
                if not isinstance(target, str) or not target or "\\" in target:
                    raise HttpSyncError(f"entries[{index}].target is invalid")
                self._validate_symlink_target(name, target, destination)
                entry["target"] = target
            entries.append(entry)
        kinds = {entry["path"]: entry["kind"] for entry in entries}
        for entry in entries:
            parent = PurePosixPath(entry["path"]).parent
            while parent != PurePosixPath("."):
                parent_kind = kinds.get(parent.as_posix())
                if parent_kind is not None and parent_kind != "directory":
                    raise HttpSyncError(
                        f"manifest path {entry['path']!r} is below non-directory "
                        f"{parent.as_posix()!r}"
                    )
                parent = parent.parent
        return entries

    def _target(self, root: Path, relative: str) -> Path:
        target = root.joinpath(*PurePosixPath(relative).parts)
        parent = target.parent.resolve(strict=False)
        root_resolved = root.resolve(strict=False)
        if not _inside(parent, root_resolved) or self._is_protected(target):
            raise HttpSyncError(f"manifest path {relative!r} is not writable")
        return target

    def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        destination = self._destination_context(payload)
        root = destination.root
        entries = self._entries(payload, destination)
        needed: list[str] = []
        for entry in entries:
            if entry["kind"] != "file":
                continue
            target = self._target(root, entry["path"])
            if (
                not target.is_file()
                or target.is_symlink()
                or target.stat().st_size != entry["size"]
                or _sha256(target) != entry["sha256"]
            ):
                needed.append(entry["path"])
        return {"schema": SYNC_MANIFEST_SCHEMA, "needed": needed}

    def apply(self, payload: dict[str, Any], uploads: dict[str, bytes]) -> None:
        destination = self._destination_context(payload)
        root = destination.root
        entries = self._entries(payload, destination)
        root.mkdir(parents=True, exist_ok=True)
        expected_files = {
            entry["path"]: entry for entry in entries if entry["kind"] == "file"
        }
        if not set(uploads).issubset(expected_files):
            raise HttpSyncError("archive contains files absent from its manifest")
        for name, data in uploads.items():
            entry = expected_files[name]
            if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise HttpSyncError(f"uploaded file {name!r} does not match its manifest")

        for name, entry in expected_files.items():
            if name in uploads:
                continue
            target = self._target(root, name)
            if (
                not target.is_file()
                or target.is_symlink()
                or target.stat().st_size != entry["size"]
                or _sha256(target) != entry["sha256"]
            ):
                raise HttpSyncError(
                    f"file {name!r} changed after planning; retry synchronization",
                    409,
                )

        directories: list[tuple[Path, int]] = []
        for entry in sorted(entries, key=lambda value: value["path"].count("/")):
            target = self._target(root, entry["path"])
            kind = entry["kind"]
            if kind == "directory":
                if (target.exists() and not target.is_dir()) or target.is_symlink():
                    self._replace_existing(target)
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(entry["mode"] | stat.S_IWUSR | stat.S_IXUSR)
                directories.append((target, entry["mode"]))
            elif kind == "symlink":
                if target.is_symlink() and os.readlink(target) == entry["target"]:
                    continue
                if target.exists() or target.is_symlink():
                    self._replace_existing(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(entry["target"])

        for name, entry in expected_files.items():
            target = self._target(root, name)
            data = uploads.get(name)
            if data is None:
                target.chmod(entry["mode"])
                continue
            if (target.exists() and target.is_dir()) or target.is_symlink():
                self._replace_existing(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                os.chmod(temporary_name, entry["mode"])
                os.replace(temporary_name, target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

        if payload.get("delete") is True:
            self._delete_extra(
                root,
                {entry["path"] for entry in entries},
                payload["excludes"],
            )
        for directory, mode in sorted(
            directories, key=lambda item: len(item[0].parts), reverse=True
        ):
            directory.chmod(mode)

    def _delete_extra(
        self, root: Path, retained: set[str], excludes: list[str]
    ) -> None:
        if not root.exists():
            return
        paths: list[Path] = []
        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name
                for name in directories
                if not self._is_protected(current_path / name)
                and not any(
                    matches_sync_exclude(
                        (current_path / name).relative_to(root).as_posix(),
                        pattern,
                        directory=True,
                    )
                    for pattern in excludes
                )
            ]
            paths.extend(current_path / name for name in directories)
            paths.extend(
                current_path / name
                for name in filenames
                if not any(
                    matches_sync_exclude(
                        (current_path / name).relative_to(root).as_posix(),
                        pattern,
                        directory=False,
                    )
                    for pattern in excludes
                )
            )
        for path in sorted(paths, key=lambda value: len(value.parts), reverse=True):
            if self._is_protected(path) or self._contains_protected(path):
                continue
            relative = path.relative_to(root).as_posix()
            if relative not in retained:
                self._replace_existing(path)

    async def plan_request(self, request: Request) -> Response:
        try:
            self._authenticate(request)
            payload = await request.json()
            if not isinstance(payload, dict):
                raise HttpSyncError("request body must be a JSON object")
            return JSONResponse(await asyncio.to_thread(self.plan, payload))
        except HttpSyncError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    async def apply_request(self, request: Request) -> Response:
        try:
            self._authenticate(request)
            body = await request.body()
            payload, uploads = await asyncio.to_thread(self._read_archive, body)
            await asyncio.to_thread(self.apply, payload, uploads)
            return JSONResponse({"ok": True})
        except HttpSyncError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        except (tarfile.TarError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse({"error": "invalid sync archive"}, status_code=400)

    def _read_archive(self, body: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
        payload: dict[str, Any] | None = None
        uploads: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    raise HttpSyncError("sync archive may contain only regular files")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise HttpSyncError("sync archive member is not readable")
                if member.name == SYNC_MANIFEST_MEMBER:
                    if payload is not None:
                        raise HttpSyncError("sync archive has multiple manifests")
                    raw = json.loads(extracted.read().decode("utf-8"))
                    if not isinstance(raw, dict):
                        raise HttpSyncError("sync archive manifest must be an object")
                    payload = raw
                    continue
                if not member.name.startswith(SYNC_FILE_PREFIX):
                    raise HttpSyncError("sync archive contains an unknown member")
                name = member.name[len(SYNC_FILE_PREFIX) :]
                relative = _relative_path(name, "archive member").as_posix()
                if relative in uploads:
                    raise HttpSyncError(f"duplicate archive member: {relative}")
                uploads[relative] = extracted.read()
        if payload is None:
            raise HttpSyncError("sync archive manifest is missing")
        return payload, uploads


def register_http_sync_routes(
    mcp: Any,
    cfg: Config,
    store: Store,
    api_key: str | None,
) -> None:
    """Register the HTTP sync protocol on a FastMCP server."""
    service = HttpSyncService(cfg, store, api_key)

    @mcp.custom_route("/sync/v1/plan", methods=["POST"])
    async def sync_plan(request: Request) -> Response:
        return await service.plan_request(request)

    @mcp.custom_route("/sync/v1/apply", methods=["POST"])
    async def sync_apply(request: Request) -> Response:
        return await service.apply_request(request)
