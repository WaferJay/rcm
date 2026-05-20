"""Run-record storage: per-run directory, capability-URL ids, retention."""

from __future__ import annotations

import json
import re
import secrets
import shutil
from pathlib import Path
from typing import Literal

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
Stream = Literal["stdout", "stderr", "meta"]


class StoreError(Exception):
    pass


class Store:
    def __init__(self, runs_dir: Path, public_base_url: str) -> None:
        self.runs_dir = runs_dir
        self.public_base_url = public_base_url.rstrip("/")
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def new_run_id() -> str:
        # ~256 bits of entropy. URL-safe.
        return secrets.token_urlsafe(32)

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise StoreError("invalid run_id")
        return self.runs_dir / run_id

    def create_run(self) -> tuple[str, Path]:
        run_id = self.new_run_id()
        d = self.runs_dir / run_id
        d.mkdir(parents=True, exist_ok=False)
        return run_id, d

    def file_path(self, run_id: str, stream: Stream) -> Path:
        d = self.run_dir(run_id)
        if stream == "stdout":
            return d / "stdout.log"
        if stream == "stderr":
            return d / "stderr.log"
        if stream == "meta":
            return d / "meta.json"
        raise StoreError(f"unknown stream {stream!r}")

    def url_for(self, run_id: str, stream: Stream) -> str:
        # capability URL: relies on run_id randomness for confidentiality.
        return f"{self.public_base_url}/runs/{run_id}/{stream}"

    def write_meta(self, run_id: str, meta: dict) -> None:
        path = self.file_path(run_id, "meta")
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def prune(self, retention: int) -> int:
        """Keep at most `retention` newest runs (by mtime). Returns count removed."""
        if retention <= 0:
            return 0
        entries = []
        for child in self.runs_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                entries.append((child.stat().st_mtime, child))
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
