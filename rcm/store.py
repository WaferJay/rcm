"""Run-record storage: per-run directory, capability-URL ids, retention."""

from __future__ import annotations

import json
import re
import secrets
import shutil
from pathlib import Path

from .artifacts import ARTIFACT_KINDS

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SCOPE_ID_RE = re.compile(r"^scope-[A-Za-z0-9_-]+$")


class StoreError(Exception):
    pass


class Store:
    def __init__(
        self,
        runs_dir: Path,
        public_base_url: str,
        *,
        local_urls: bool = False,
    ) -> None:
        self.runs_dir = runs_dir
        self.public_base_url = public_base_url.rstrip("/")
        self.local_urls = local_urls
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def new_run_id() -> str:
        # ~256 bits of entropy. URL-safe.
        return secrets.token_urlsafe(32)

    def _scope_dir(self, scope_id: str | None) -> Path:
        if scope_id is None:
            return self.runs_dir
        if not SCOPE_ID_RE.fullmatch(scope_id):
            raise StoreError("invalid scope_id")
        return self.runs_dir / scope_id

    def run_dir(self, run_id: str, scope_id: str | None = None) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise StoreError("invalid run_id")
        return self._scope_dir(scope_id) / run_id

    def create_run(self, scope_id: str | None = None) -> tuple[str, Path]:
        run_id = self.new_run_id()
        d = self.run_dir(run_id, scope_id)
        d.mkdir(parents=True, exist_ok=False)
        return run_id, d

    def create_staging_run(self, scope_id: str | None = None) -> tuple[str, Path]:
        """Create an unpublished run directory for atomic proxy localization."""
        parent = self._scope_dir(scope_id)
        parent.mkdir(parents=True, exist_ok=True)
        while True:
            run_id = self.new_run_id()
            staging = parent / f".{run_id}.tmp"
            try:
                staging.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                continue
            return run_id, staging

    def commit_staging_run(
        self, run_id: str, staging: Path, meta: dict, scope_id: str | None = None
    ) -> Path:
        """Write metadata and atomically publish a staged run."""
        expected = self._scope_dir(scope_id) / f".{run_id}.tmp"
        if staging != expected or not staging.is_dir():
            raise StoreError("invalid staging run")
        meta_path = staging / "meta.json"
        tmp = staging / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(meta_path)
        destination = self.run_dir(run_id, scope_id)
        if destination.exists():
            raise StoreError("run_id already exists")
        staging.replace(destination)
        return destination

    def file_path(self, run_id: str, name: str, scope_id: str | None = None) -> Path:
        d = self.run_dir(run_id, scope_id)
        if name == "meta":
            return d / "meta.json"
        kind = ARTIFACT_KINDS.get(name)
        if kind is None:
            raise StoreError(f"unknown stored file {name!r}")
        return d / kind.filename

    def url_for(self, run_id: str, name: str, scope_id: str | None = None) -> str:
        if self.local_urls:
            return self.file_path(run_id, name, scope_id).resolve().as_uri()
        # capability URL: relies on run_id randomness for confidentiality.
        if scope_id is not None:
            self._scope_dir(scope_id)
            return f"{self.public_base_url}/runs/{scope_id}/{run_id}/{name}"
        return f"{self.public_base_url}/runs/{run_id}/{name}"

    def write_meta(self, run_id: str, meta: dict, scope_id: str | None = None) -> None:
        path = self.file_path(run_id, "meta", scope_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def prune(self, retention: int) -> int:
        """Keep at most `retention` newest runs (by mtime). Returns count removed."""
        if retention <= 0:
            return 0
        entries = []
        for child in self.runs_dir.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            candidates = (
                child.iterdir() if SCOPE_ID_RE.fullmatch(child.name) else (child,)
            )
            for candidate in candidates:
                if not candidate.is_dir() or candidate.name.startswith("."):
                    continue
                try:
                    entries.append((candidate.stat().st_mtime, candidate))
                except OSError:
                    continue
        entries.sort(reverse=True)
        removed = 0
        for _, child in entries[retention:]:
            try:
                shutil.rmtree(child)
                removed += 1
            except OSError:
                pass
        return removed
