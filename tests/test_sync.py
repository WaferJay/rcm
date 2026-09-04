"""Tests for active proxy synchronization."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rcm.config import (
    ProxyTargetSpec,
    RemoteConfigSpec,
    SSHSpec,
    SyncMappingSpec,
    SyncSpec,
)
from rcm.sync import SyncError, SyncRunner


def _sync(*mappings: SyncMappingSpec) -> SyncSpec:
    return SyncSpec(mappings=list(mappings))


def test_sync_spec_accepts_legacy_constructor_fields() -> None:
    spec = SyncSpec(
        source="./source",
        destination="destination",
        excludes=["build/**"],
        delete=True,
    )

    assert len(spec.mappings) == 1
    assert spec.source == "./source"
    assert spec.destination == "destination"
    assert spec.excludes == ["build/**"]
    assert spec.delete is True


@pytest.mark.asyncio
async def test_sync_excludes_config_runs_and_glob_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "commands.yaml").write_text("local secret\n", encoding="utf-8")
    (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "cache.pyc").write_bytes(b"cache")
    (source / "runs").mkdir()
    (source / "runs" / "new-output").write_text("new\n", encoding="utf-8")
    (destination / "commands.yaml").write_text("remote secret\n", encoding="utf-8")
    (destination / "runs").mkdir()
    (destination / "runs" / "old-output").write_text("old\n", encoding="utf-8")

    target = ProxyTargetSpec(
        name="compile",
        transport="stdio",
        command=["echo"],
        sync=_sync(
            SyncMappingSpec(
                source=str(source),
                destination=str(destination),
                excludes=["**/*.pyc"],
                delete=True,
            )
        ),
    )
    monkeypatch.setenv("RCM_RUNS_DIR", str(source / "runs"))
    runner = SyncRunner(target, config_path=source / "commands.yaml")
    await runner.sync()

    assert (destination / "main.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert (destination / "commands.yaml").read_text(encoding="utf-8") == "remote secret\n"
    assert (destination / "runs" / "old-output").read_text(encoding="utf-8") == "old\n"
    assert not (destination / "runs" / "new-output").exists()
    assert not (destination / "nested" / "cache.pyc").exists()


@pytest.mark.asyncio
async def test_sync_runs_mappings_in_order(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "destination"
    first.mkdir()
    second.mkdir()
    (first / "value.txt").write_text("first", encoding="utf-8")
    (second / "value.txt").write_text("second", encoding="utf-8")

    target = ProxyTargetSpec(
        name="compile",
        transport="stdio",
        command=["echo"],
        sync=_sync(
            SyncMappingSpec(source=str(first), destination=str(destination)),
            SyncMappingSpec(source=str(second), destination=str(destination)),
        ),
    )
    await SyncRunner(target).sync()

    assert (destination / "value.txt").read_text(encoding="utf-8") == "second"


def test_ssh_sync_destination_uses_target_host(tmp_path: Path) -> None:
    mapping = SyncMappingSpec(source=str(tmp_path), destination="remote/project")
    target = ProxyTargetSpec(
        name="compile",
        transport="ssh",
        ssh=SSHSpec(host="compile-machine", command=["rcm", "--stdio"]),
        sync=_sync(mapping),
    )
    runner = SyncRunner(target)
    command = runner._command(mapping, tmp_path)
    assert command[-1] == "compile-machine:remote/project/"


def test_http_remote_config_sync_destination_uses_ssh_host(tmp_path: Path) -> None:
    mapping = SyncMappingSpec(source=str(tmp_path), destination="/remote/project")
    target = ProxyTargetSpec(
        name="remote-http",
        transport="http",
        endpoint="https://remote.example.com/mcp",
        ssh=SSHSpec(host="compile-machine"),
        sync=_sync(mapping),
    )
    runner = SyncRunner(target)
    command = runner._command(mapping, tmp_path)
    assert command[-1] == "compile-machine:/remote/project/"


def test_remote_config_and_standard_runs_are_always_protected(tmp_path: Path) -> None:
    mapping = SyncMappingSpec(
        source=str(tmp_path),
        destination="/etc/rcm",
        excludes=["build/**"],
    )
    target = ProxyTargetSpec(
        name="remote",
        transport="http",
        endpoint="https://remote.example.com/mcp",
        ssh=SSHSpec(host="compile-machine"),
        remote_config=RemoteConfigSpec(path="/etc/rcm/commands.yaml"),
        sync=_sync(mapping),
    )

    command = SyncRunner(target)._command(mapping, tmp_path)

    patterns = [
        command[index + 1]
        for index, part in enumerate(command)
        if part == "--exclude"
    ]
    assert patterns == ["build/**", "/commands.yaml", "/runs/"]


def test_overlapping_destinations_with_delete_are_rejected(tmp_path: Path) -> None:
    target = ProxyTargetSpec(
        name="compile",
        transport="ssh",
        ssh=SSHSpec(host="compile-machine", command=["rcm", "--stdio"]),
        sync=_sync(
            SyncMappingSpec(source=str(tmp_path), destination="/srv/project"),
            SyncMappingSpec(
                source=str(tmp_path),
                destination="/srv/project/build",
                delete=True,
            ),
        ),
    )

    with pytest.raises(SyncError, match="overlapping destinations"):
        SyncRunner(target)


def test_overlapping_destinations_without_delete_are_allowed(tmp_path: Path) -> None:
    target = ProxyTargetSpec(
        name="compile",
        transport="ssh",
        ssh=SSHSpec(host="compile-machine", command=["rcm", "--stdio"]),
        sync=_sync(
            SyncMappingSpec(source=str(tmp_path), destination="/srv/project"),
            SyncMappingSpec(source=str(tmp_path), destination="/srv/project/build"),
        ),
    )

    SyncRunner(target)


@pytest.mark.asyncio
async def test_source_inside_runs_directory_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    source = runs / "one-run"
    source.mkdir(parents=True)
    monkeypatch.setenv("RCM_RUNS_DIR", str(runs))
    target = ProxyTargetSpec(
        name="compile",
        transport="stdio",
        command=["echo"],
        sync=_sync(
            SyncMappingSpec(source=str(source), destination=str(tmp_path / "dest"))
        ),
    )

    with pytest.raises(SyncError, match="inside the protected runs directory"):
        await SyncRunner(target).sync()


@pytest.mark.asyncio
async def test_mapping_failure_stops_later_mappings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    target = ProxyTargetSpec(
        name="compile",
        transport="stdio",
        command=["echo"],
        sync=_sync(
            SyncMappingSpec(source=str(first), destination=str(tmp_path / "dest-1")),
            SyncMappingSpec(source=str(second), destination=str(tmp_path / "dest-2")),
        ),
    )
    calls: list[tuple[str, ...]] = []

    class FailedProcess:
        returncode = 23

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"permission denied"

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return FailedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr("rcm.sync.shutil.which", lambda _: "/usr/bin/rsync")

    with pytest.raises(SyncError, match=r"mapping 1.*first.*dest-1.*permission denied"):
        await SyncRunner(target).sync()
    assert len(calls) == 1
