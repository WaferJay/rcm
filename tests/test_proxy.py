"""Tests for proxied MCP tool forwarding."""

from __future__ import annotations

import asyncio
import socket

import pytest
from fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from rcm.config import AuthSpec, Config, DefaultsSpec, ProxySpec, ProxyTargetSpec, ServerSpec
from rcm.proxy import ProxyTool
from rcm.server import build_proxy_server
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
async def test_proxy_runtime_discovers_and_registers_stdio_tools(tmp_path) -> None:
    import sys

    remote_code = """
from fastmcp import FastMCP

mcp = FastMCP('remote')

@mcp.tool
def remote_echo(value: str) -> str:
    return value

mcp.run()
"""
    cfg = Config(
        server=ServerSpec(public_base_url="http://testserver"),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[],
        mode="proxy",
        proxy=ProxySpec(
            targets=[
                ProxyTargetSpec(
                    name="compile",
                    transport="stdio",
                    command=[sys.executable, "-c", remote_code],
                )
            ]
        ),
    )
    mcp, runtime = await build_proxy_server(
        cfg,
        Store(tmp_path / "runs", "http://testserver"),
        "key",
    )
    try:
        tools = await mcp._list_tools()
        assert [tool.name for tool in tools] == ["compile__remote_echo"]
        tool = await mcp.get_tool("compile__remote_echo")
        assert tool is not None
        result = await tool.run({"value": "ok"})
        assert result.content[0].text == "ok"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_proxy_runtime_bridges_streamable_http(tmp_path) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    remote = FastMCP("remote-http")

    @remote.tool
    def remote_echo(value: str) -> str:
        return value

    remote_task = asyncio.create_task(
        remote.run_async(
            transport="http",
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("remote HTTP server failed to start")
            await asyncio.sleep(0.05)

    cfg = Config(
        server=ServerSpec(public_base_url="http://testserver"),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[],
        mode="proxy",
        proxy=ProxySpec(
            targets=[
                ProxyTargetSpec(
                    name="reports",
                    transport="http",
                    endpoint=f"http://127.0.0.1:{port}/mcp",
                )
            ]
        ),
    )
    runtime = None
    try:
        _, runtime = await build_proxy_server(
            cfg,
            Store(tmp_path / "runs", "http://testserver"),
            "key",
        )
        tool = await runtime.server.get_tool("reports__remote_echo")
        assert tool is not None
        result = await tool.run({"value": "ok"})
        assert result.content[0].text == "ok"
    finally:
        if runtime is not None:
            await runtime.close()
        remote_task.cancel()
        try:
            await remote_task
        except BaseException:
            pass
