"""Tests for the authenticated HTTP synchronization protocol."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

from rcm.config import (
    AuthSpec,
    CommandSpec,
    Config,
    DefaultsSpec,
    HeaderSpec,
    IsolationSpec,
    ProxyTargetSpec,
    ServerSpec,
    SyncMappingSpec,
    SyncSpec,
)
from rcm.http_sync import HttpSyncError, HttpSyncService, SYNC_MANIFEST_SCHEMA
from rcm.store import Store
from rcm.sync import SyncRunner
from rcm.workspace import Workspace


def _config(root: Path, *, config_path: Path | None = None) -> Config:
    return Config(
        server=ServerSpec(),
        auth=AuthSpec(),
        defaults=DefaultsSpec(cwd=str(root)),
        commands=[],
        config_path=config_path,
    )


def _payload(**changes) -> dict:
    payload = {
        "schema": SYNC_MANIFEST_SCHEMA,
        "destination": ".",
        "delete": False,
        "excludes": [],
        "entries": [],
    }
    payload.update(changes)
    return payload


@pytest.mark.asyncio
async def test_http_sync_uploads_only_changed_files_and_applies_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote"
    runs = remote / "runs"
    source.mkdir()
    remote.mkdir()
    runs.mkdir()
    (source / "main.py").write_text("one\n", encoding="utf-8")
    (source / "ignored.pyc").write_bytes(b"cache")
    (source / "empty").mkdir()
    (remote / "old.txt").write_text("old\n", encoding="utf-8")
    (remote / "ignored.pyc").write_bytes(b"remote cache")
    remote_config = remote / "commands.yaml"
    remote_config.write_text("secret\n", encoding="utf-8")
    (runs / "result.log").write_text("keep\n", encoding="utf-8")

    service = HttpSyncService(
        _config(remote, config_path=remote_config),
        Store(runs, runs.as_uri(), local_urls=True),
        "key",
    )
    planned: list[list[str]] = []
    authorization: list[str | None] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        authorization.append(request.headers.get("authorization"))
        if request.url.path == "/sync/v1/plan":
            result = service.plan(json.loads(request.content))
            planned.append(result["needed"])
            return httpx.Response(200, json=result)
        payload, uploads = service._read_archive(request.content)
        service.apply(payload, uploads)
        return httpx.Response(200, json={"ok": True})

    original_client = httpx.AsyncClient

    def mock_client(**kwargs):
        return original_client(
            headers=kwargs.get("headers"),
            timeout=kwargs.get("timeout"),
            transport=httpx.MockTransport(handle),
        )

    monkeypatch.setattr("rcm.sync.httpx.AsyncClient", mock_client)
    target = ProxyTargetSpec(
        name="remote",
        transport="http",
        endpoint="https://remote.example/mcp",
        headers={"Authorization": HeaderSpec(value="Bearer key")},
        sync=SyncSpec(
            mappings=[
                SyncMappingSpec(
                    source=str(source),
                    destination=".",
                    excludes=["**/*.pyc"],
                    delete=True,
                )
            ]
        ),
    )
    runner = SyncRunner(target)

    await runner.sync()
    await runner.sync()
    (source / "main.py").write_text("two\n", encoding="utf-8")
    await runner.sync()

    assert planned == [["main.py"], [], ["main.py"]]
    assert authorization == ["Bearer key"] * 6
    assert (remote / "main.py").read_text(encoding="utf-8") == "two\n"
    assert (remote / "empty").is_dir()
    assert (remote / "ignored.pyc").read_bytes() == b"remote cache"
    assert not (remote / "old.txt").exists()
    assert remote_config.read_text(encoding="utf-8") == "secret\n"
    assert (runs / "result.log").read_text(encoding="utf-8") == "keep\n"


def test_http_sync_rejects_unsafe_paths_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    runs = tmp_path / "runs"
    root.mkdir()
    service = HttpSyncService(
        _config(root), Store(runs, runs.as_uri(), local_urls=True), "key"
    )

    with pytest.raises(HttpSyncError, match="relative POSIX"):
        service.plan(_payload(destination="../escape"))
    with pytest.raises(HttpSyncError, match="safe relative"):
        service.plan(
            _payload(entries=[{"path": "a/../escape", "kind": "directory", "mode": 0o755}])
        )
    with pytest.raises(HttpSyncError, match="escapes"):
        service.plan(
            _payload(
                entries=[
                    {
                        "path": "link",
                        "kind": "symlink",
                        "mode": 0o777,
                        "target": "../outside",
                    }
                ]
            )
        )
    with pytest.raises(HttpSyncError, match="allowed synchronization root"):
        service.plan(
            _payload(
                destination="a",
                entries=[
                    {
                        "path": "link",
                        "kind": "symlink",
                        "mode": 0o777,
                        "target": "../../outside",
                    }
                ],
            )
        )
    with pytest.raises(HttpSyncError, match="allowed synchronization root"):
        service.plan(
            _payload(
                destination="a",
                entries=[
                    {
                        "path": "link",
                        "kind": "symlink",
                        "mode": 0o777,
                        "target": "/outside",
                    }
                ],
            )
        )
    assert service.plan(
        _payload(
            destination="a",
            entries=[
                {
                    "path": "root",
                    "kind": "symlink",
                    "mode": 0o777,
                    "target": "..",
                }
            ],
        )
    ) == {"schema": SYNC_MANIFEST_SCHEMA, "needed": []}

    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
        info = tarfile.TarInfo("files/../escape")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(HttpSyncError, match="safe relative"):
        service._read_archive(archive_bytes.getvalue())


@pytest.mark.asyncio
async def test_http_sync_preserves_symlink_across_mapping_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source_a = source / "a"
    source_c = source / "c"
    source_a.mkdir(parents=True)
    (source_c / "d").mkdir(parents=True)
    (source_c / "d" / "value.txt").write_text("shared\n", encoding="utf-8")
    (source_a / "b").symlink_to("../c/d", target_is_directory=True)

    remote = tmp_path / "remote"
    runs = tmp_path / "runs"
    remote.mkdir()
    service = HttpSyncService(
        _config(remote), Store(runs, runs.as_uri(), local_urls=True), None
    )
    planned_destinations: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sync/v1/plan":
            payload = json.loads(request.content)
            planned_destinations.append(payload["destination"])
            if payload["destination"] == "a":
                assert not (remote / "c").exists()
            return httpx.Response(200, json=service.plan(payload))
        payload, uploads = service._read_archive(request.content)
        service.apply(payload, uploads)
        return httpx.Response(200, json={"ok": True})

    original_client = httpx.AsyncClient

    def mock_client(**kwargs):
        return original_client(
            headers=kwargs.get("headers"),
            timeout=kwargs.get("timeout"),
            transport=httpx.MockTransport(handle),
        )

    monkeypatch.setattr("rcm.sync.httpx.AsyncClient", mock_client)
    target = ProxyTargetSpec(
        name="remote",
        transport="http",
        endpoint="https://remote.example/mcp",
        sync=SyncSpec(
            mappings=[
                SyncMappingSpec(source=str(source_a), destination="a", delete=True),
                SyncMappingSpec(source=str(source_c), destination="c", delete=True),
            ]
        ),
    )

    await SyncRunner(target).sync()

    link = remote / "a" / "b"
    assert planned_destinations == ["a", "c"]
    assert link.is_symlink()
    assert link.readlink() == Path("../c/d")
    assert link.resolve() == (remote / "c" / "d").resolve()
    assert (link / "value.txt").read_text(encoding="utf-8") == "shared\n"

    service.apply(
        _payload(
            destination="a",
            delete=True,
            entries=[
                {
                    "path": "b",
                    "kind": "symlink",
                    "mode": 0o777,
                    "target": "../c/d",
                }
            ],
        ),
        {},
    )
    assert (remote / "c" / "d" / "value.txt").read_text(encoding="utf-8") == (
        "shared\n"
    )


def test_http_sync_cannot_replace_parent_of_protected_runs(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    runs = root / "state" / "runs"
    runs.mkdir(parents=True)
    service = HttpSyncService(
        _config(root), Store(runs, runs.as_uri(), local_urls=True), None
    )
    data = b"replacement"
    payload = _payload(
        entries=[
            {
                "path": "state",
                "kind": "file",
                "mode": 0o644,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ]
    )
    with pytest.raises(HttpSyncError, match="protected RCM data"):
        service.apply(payload, {"state": data})
    assert runs.is_dir()


def test_http_sync_authentication_is_required(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    root.mkdir()
    runs = tmp_path / "runs"
    service = HttpSyncService(
        _config(root), Store(runs, runs.as_uri(), local_urls=True), "secret"
    )

    with pytest.raises(HttpSyncError, match="unauthorized"):
        service._authenticate(Request({"type": "http", "headers": []}))
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer secret")],
        }
    )
    service._authenticate(request)


def test_http_sync_accepts_only_configured_workspace_roots(tmp_path: Path) -> None:
    default_root = tmp_path / "default"
    workspace_root = tmp_path / "workspaces"
    runs = tmp_path / "runs"
    default_root.mkdir()
    workspace_root.mkdir()
    cfg = _config(default_root)
    cfg.commands = [
        CommandSpec(
            name="isolated",
            description="isolated",
            command=["true"],
            isolate=IsolationSpec(by="session", base_dir=str(workspace_root)),
        )
    ]
    service = HttpSyncService(
        cfg, Store(runs, runs.as_uri(), local_urls=True), None
    )
    payload = _payload(
        destination="src",
        scope_id="scope-client",
        workspace_base=str(workspace_root),
    )
    service.apply(payload, {})
    assert (workspace_root / "scope-client" / "src").is_dir()

    cross_mapping = _payload(
        destination="a",
        scope_id="scope-client",
        workspace_base=str(workspace_root),
        entries=[
            {
                "path": "b",
                "kind": "symlink",
                "mode": 0o777,
                "target": "../c/d",
            }
        ],
    )
    service.apply(cross_mapping, {})
    assert (workspace_root / "scope-client" / "a" / "b").readlink() == Path(
        "../c/d"
    )

    cross_scope = dict(cross_mapping)
    cross_scope["entries"] = [
        {
            "path": "b",
            "kind": "symlink",
            "mode": 0o777,
            "target": "../../scope-other/d",
        }
    ]
    with pytest.raises(HttpSyncError, match="allowed synchronization root"):
        service.plan(cross_scope)

    payload["workspace_base"] = str(tmp_path / "other")
    with pytest.raises(HttpSyncError, match="not an allowed"):
        service.plan(payload)


def test_http_runner_serializes_workspace_scope(tmp_path: Path) -> None:
    target = ProxyTargetSpec(
        name="remote",
        transport="http",
        endpoint="https://remote.example/prefix/mcp/",
        sync=SyncSpec(
            mappings=[
                SyncMappingSpec(
                    source=str(tmp_path),
                    destination="source",
                    workspace_destination="workspace-source",
                )
            ]
        ),
    )
    runner = SyncRunner(target)
    mapping = target.sync.mappings[0]
    payload, _ = runner._http_manifest(
        mapping,
        tmp_path,
        Workspace("scope-client", Path("/srv/workspaces")),
    )

    assert payload["destination"] == "workspace-source"
    assert payload["scope_id"] == "scope-client"
    assert payload["workspace_base"] == "/srv/workspaces"
    assert runner._http_endpoint("/sync/v1/plan") == (
        "https://remote.example/prefix/sync/v1/plan"
    )
