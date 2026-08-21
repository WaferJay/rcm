"""Tests for rcm.store: run_id generation, path safety, retention."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from rcm.store import RUN_ID_RE, Store, StoreError


def test_new_run_id_is_url_safe_and_unique() -> None:
    ids = {Store.new_run_id() for _ in range(50)}
    assert len(ids) == 50
    for rid in ids:
        assert RUN_ID_RE.fullmatch(rid)
        # token_urlsafe(32) yields ~43 base64url chars.
        assert len(rid) >= 32


def test_url_for_strips_trailing_slash(tmp_path: Path) -> None:
    s = Store(tmp_path, public_base_url="https://x.example/")
    assert s.url_for("abc", "stdout") == "https://x.example/runs/abc/stdout"


def test_url_for_each_stream(tmp_path: Path) -> None:
    s = Store(tmp_path, public_base_url="https://x")
    assert s.url_for("a", "stdout").endswith("/runs/a/stdout")
    assert s.url_for("a", "stderr").endswith("/runs/a/stderr")
    assert s.url_for("a", "meta").endswith("/runs/a/meta")


def test_local_url_for_uses_absolute_file_uri(tmp_path: Path) -> None:
    s = Store(tmp_path, public_base_url=tmp_path.as_uri(), local_urls=True)
    rid, _ = s.create_run()
    assert s.url_for(rid, "stdout") == s.file_path(rid, "stdout").resolve().as_uri()


def test_create_run_makes_directory(tmp_path: Path) -> None:
    s = Store(tmp_path, public_base_url="http://x")
    rid, d = s.create_run()
    assert RUN_ID_RE.fullmatch(rid)
    assert d.is_dir()
    assert d.parent == tmp_path


def test_file_path_rejects_path_traversal(tmp_path: Path) -> None:
    s = Store(tmp_path, public_base_url="http://x")
    for bad in ["..", "../etc", "a/b", "a b", "", "a/"]:
        with pytest.raises(StoreError):
            s.file_path(bad, "stdout")


def test_file_path_unknown_stream(tmp_path: Path) -> None:
    s = Store(tmp_path, public_base_url="http://x")
    rid, _ = s.create_run()
    with pytest.raises(StoreError):
        s.file_path(rid, "weird")  # type: ignore[arg-type]


def test_write_meta_atomic(tmp_path: Path) -> None:
    s = Store(tmp_path, public_base_url="http://x")
    rid, _ = s.create_run()
    s.write_meta(rid, {"run_id": rid, "x": 1})
    data = json.loads(s.file_path(rid, "meta").read_text(encoding="utf-8"))
    assert data == {"run_id": rid, "x": 1}
    # No leftover tmp file.
    assert not (s.run_dir(rid) / "meta.json.tmp").exists()


def test_prune_keeps_newest(tmp_path: Path) -> None:
    s = Store(tmp_path, public_base_url="http://x")
    created = []
    for _ in range(5):
        rid, d = s.create_run()
        created.append((rid, d))
        time.sleep(0.01)
    # Bump mtime of the last 2 to be newest deterministically.
    now = time.time()
    for i, (_, d) in enumerate(created):
        os.utime(d, (now - (5 - i), now - (5 - i)))

    removed = s.prune(retention=2)
    assert removed == 3

    survivors = {d.name for d in tmp_path.iterdir() if d.is_dir()}
    assert survivors == {created[-1][0], created[-2][0]}


def test_prune_zero_is_noop(tmp_path: Path) -> None:
    s = Store(tmp_path, public_base_url="http://x")
    s.create_run()
    s.create_run()
    assert s.prune(0) == 0
    assert sum(1 for _ in tmp_path.iterdir()) == 2
