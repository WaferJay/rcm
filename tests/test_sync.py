"""Tests for active proxy synchronization."""

from __future__ import annotations

from pathlib import Path

import pytest

from rcm.config import ProxyTargetSpec, SyncSpec
from rcm.sync import SyncRunner


@pytest.mark.asyncio
async def test_sync_excludes_config_and_glob_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "commands.yaml").write_text("secret\n", encoding="utf-8")
    (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "cache.pyc").write_bytes(b"cache")

    target = ProxyTargetSpec(
        name="compile",
        transport="stdio",
        command=["echo"],
        sync=SyncSpec(
            source=str(source),
            destination=str(destination),
            excludes=["**/*.pyc"],
        ),
    )
    monkeypatch.setenv("RCM_CONFIG", str(source / "commands.yaml"))
    runner = SyncRunner(target)
    await runner.sync()

    assert (destination / "main.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert not (destination / "commands.yaml").exists()
    assert not (destination / "nested" / "cache.pyc").exists()


def test_ssh_sync_destination_uses_target_host(tmp_path: Path) -> None:
    from rcm.config import SSHSpec

    target = ProxyTargetSpec(
        name="compile",
        transport="ssh",
        ssh=SSHSpec(host="compile-machine", command=["rcm", "--stdio"]),
        sync=SyncSpec(source=str(tmp_path), destination="/remote/project"),
    )
    runner = SyncRunner(target)
    command = runner._command(tmp_path)
    assert command[-1] == "compile-machine:/remote/project/"
