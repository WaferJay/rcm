"""Tests for proxied MCP tool forwarding and result localization."""

from __future__ import annotations

import gzip
import hashlib
import json

import pytest
from mcp.types import CallToolResult, TextContent

from rcm.proxy import ProxyTool
from rcm.store import Store
from rcm.sync import SyncError


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool_mcp(self, name: str, arguments: dict) -> CallToolResult:
        self.calls.append((name, arguments))
        return CallToolResult(
            content=[TextContent(type="text", text="remote result")],
        )


class FakeSync:
    def __init__(self) -> None:
        self.calls = 0

    async def sync(self) -> None:
        self.calls += 1


@pytest.mark.asyncio
async def test_proxy_tool_syncs_before_forwarding() -> None:
    client = FakeClient()
    sync = FakeSync()
    tool = ProxyTool(
        public_name="compile__build",
        target_name="compile",
        remote_name="build",
        description="Build remotely.",
        parameters={"type": "object", "properties": {"release": {"type": "boolean"}}},
        output_schema=None,
        client=client,
        sync=sync,
    )

    result = await tool.run({"release": True})

    assert sync.calls == 1
    assert client.calls == [("build", {"release": True})]
    assert result.content[0].text == "remote result"


@pytest.mark.asyncio
async def test_proxy_tool_blocks_call_when_sync_fails() -> None:
    client = FakeClient()

    class FailingSync:
        async def sync(self) -> None:
            raise SyncError("sync failed")

    tool = ProxyTool(
        public_name="compile__build",
        target_name="compile",
        remote_name="build",
        description=None,
        parameters={"type": "object", "properties": {}},
        output_schema=None,
        client=client,
        sync=FailingSync(),
    )

    with pytest.raises(Exception, match="sync failed"):
        await tool.run({})
    assert client.calls == []


@pytest.mark.asyncio
async def test_proxy_tool_reports_tunneled_connection_failure() -> None:
    failures: list[Exception] = []

    class FailingClient:
        async def call_tool_mcp(self, name: str, arguments: dict):
            raise RuntimeError("connection lost")

    tool = ProxyTool(
        public_name="compile__build",
        target_name="compile",
        remote_name="build",
        description=None,
        parameters={"type": "object", "properties": {}},
        output_schema=None,
        client=FailingClient(),
        sync=None,
        failure_reporter=failures.append,
    )

    with pytest.raises(Exception, match="connection lost"):
        await tool.run({})
    assert len(failures) == 1
    assert str(failures[0]) == "connection lost"


@pytest.mark.asyncio
async def test_proxy_tool_localizes_stdio_rcm_files(tmp_path) -> None:
    stdout = b"\x00remote\xff\n"
    stderr = b"warning\n"
    collect = gzip.compress(b"collected tar bytes", mtime=0)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    stdout_path = upstream / "stdout.log"
    stderr_path = upstream / "stderr.log"
    collect_path = upstream / "collect.tar.gz"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    collect_path.write_bytes(collect)

    class ArtifactClient:
        async def call_tool_mcp(
            self, name: str, arguments: dict, *, meta: dict
        ) -> CallToolResult:
            assert meta == {"rcm": {"artifacts": {"version": 2}}}
            return CallToolResult(
                content=[],
                structuredContent={
                    "schema": "rcm.run-result/v2",
                    "run_id": "remote-run",
                    "returncode": 0,
                    "timed_out": False,
                    "duration_ms": 12,
                    "stdout": {
                        "uri": stdout_path.as_uri(),
                        "bytes": len(stdout),
                        "sha256": hashlib.sha256(stdout).hexdigest(),
                    },
                    "stderr": {
                        "uri": stderr_path.as_uri(),
                        "bytes": len(stderr),
                        "sha256": hashlib.sha256(stderr).hexdigest(),
                    },
                    "collect": {
                        "uri": collect_path.as_uri(),
                        "bytes": len(collect),
                        "sha256": hashlib.sha256(collect).hexdigest(),
                    },
                    "warnings": [
                        {
                            "code": "collect_path_missing",
                            "path": "optional.txt",
                            "required": False,
                            "message": "optional collect path does not exist",
                        }
                    ],
                    "extension": {"preserved": True},
                },
            )

    store = Store(tmp_path / "runs", "https://outer.example", local_urls=False)
    tool = ProxyTool(
        public_name="remote__build",
        target_name="remote",
        remote_name="build",
        description=None,
        parameters={"type": "object", "properties": {}},
        output_schema=None,
        client=ArtifactClient(),
        sync=None,
        store=store,
        target_transport="stdio",
        rcm_peer=True,
    )

    result = await tool.run({})
    data = result.structured_content
    assert data["run_id"] != "remote-run"
    assert data["schema"] == "rcm.run-result/v2"
    assert data["stdout"]["uri"].startswith("https://outer.example/runs/")
    assert data["stderr"]["uri"].startswith("https://outer.example/runs/")
    assert store.file_path(data["run_id"], "stdout").read_bytes() == stdout
    assert store.file_path(data["run_id"], "stderr").read_bytes() == stderr
    assert store.file_path(data["run_id"], "collect").name == "collect.tar.gz"
    assert store.file_path(data["run_id"], "collect").read_bytes() == collect
    assert data["warnings"][0]["path"] == "optional.txt"
    assert data["extension"] == {"preserved": True}
    assert json.loads(result.content[0].text) == data


@pytest.mark.asyncio
async def test_http_rcm_passthrough_keeps_remote_result(tmp_path) -> None:
    remote = {
        "schema": "rcm.run-result/v2",
        "run_id": "remote-run",
        "returncode": 0,
        "timed_out": False,
        "duration_ms": 8,
        "stdout": {
            "uri": "https://remote.example/runs/remote-run/stdout",
            "bytes": 3,
            "sha256": hashlib.sha256(b"out").hexdigest(),
        },
        "stderr": {
            "uri": "https://remote.example/runs/remote-run/stderr",
            "bytes": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
        },
        "collect": {
            "uri": "https://remote.example/runs/remote-run/collect",
            "bytes": 3,
            "sha256": hashlib.sha256(b"tar").hexdigest(),
        },
    }

    class RcmClient:
        async def call_tool_mcp(self, name, arguments, *, meta):
            return CallToolResult(
                content=[TextContent(type="text", text="stale")],
                structuredContent=remote,
            )

    store = Store(tmp_path / "runs", "https://local.example", local_urls=False)
    tool = ProxyTool(
        public_name="reports__build",
        target_name="reports",
        remote_name="build",
        description=None,
        parameters={"type": "object", "properties": {}},
        output_schema=None,
        client=RcmClient(),
        sync=None,
        store=store,
        target_transport="http",
        artifact_mode="passthrough",
        rcm_peer=True,
    )

    result = await tool.run({})

    assert result.structured_content == remote
    assert json.loads(result.content[0].text) == remote
    assert list(store.runs_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_generic_result_from_rcm_aggregator_is_unchanged() -> None:
    original = CallToolResult(
        content=[TextContent(type="text", text="ordinary result")],
        structuredContent={"value": 7},
        _meta={"remote": True},
    )

    class RcmClient:
        async def call_tool_mcp(self, name, arguments, *, meta):
            return original

    tool = ProxyTool(
        public_name="aggregate__ordinary",
        target_name="aggregate",
        remote_name="ordinary",
        description=None,
        parameters={"type": "object", "properties": {}},
        output_schema=None,
        client=RcmClient(),
        sync=None,
        rcm_peer=True,
    )

    result = await tool.run({})

    assert result.content == original.content
    assert result.structured_content == {"value": 7}
    assert result.meta == {"remote": True}


@pytest.mark.asyncio
async def test_failed_localization_removes_staging_run(tmp_path) -> None:
    source = tmp_path / "source.log"
    source.write_bytes(b"actual")
    remote = {
        "schema": "rcm.run-result/v2",
        "run_id": "remote-run",
        "returncode": 0,
        "timed_out": False,
        "duration_ms": 1,
        "stdout": {
            "uri": source.as_uri(),
            "bytes": 6,
            "sha256": hashlib.sha256(b"actual").hexdigest(),
        },
        "stderr": {
            "uri": source.as_uri(),
            "bytes": 6,
            "sha256": hashlib.sha256(b"actual").hexdigest(),
        },
        "collect": {
            "uri": source.as_uri(),
            "bytes": 6,
            "sha256": "0" * 64,
        },
    }

    class RcmClient:
        async def call_tool_mcp(self, name, arguments, *, meta):
            return CallToolResult(content=[], structuredContent=remote)

    store = Store(tmp_path / "runs", (tmp_path / "runs").as_uri(), local_urls=True)
    tool = ProxyTool(
        public_name="remote__build",
        target_name="remote",
        remote_name="build",
        description=None,
        parameters={"type": "object", "properties": {}},
        output_schema=None,
        client=RcmClient(),
        sync=None,
        store=store,
        target_transport="stdio",
        rcm_peer=True,
    )

    with pytest.raises(Exception, match="SHA-256 mismatch"):
        await tool.run({})
    assert list(store.runs_dir.iterdir()) == []

