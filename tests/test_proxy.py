"""Tests for proxied MCP tool forwarding."""

from __future__ import annotations

import asyncio
import base64
import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from rcm.config import (
    AuthSpec,
    CommandSpec,
    Config,
    DefaultsSpec,
    HeaderSpec,
    ProxySpec,
    ProxyTargetSpec,
    RemoteConfigSpec,
    SSHSpec,
    ServerSpec,
)
from rcm.proxy import (
    ProxyTool,
    RemoteServerMetadata,
    _discover_remote_stdio_command,
    _read_remote_metadata,
    _resolve_remote_target,
)
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
async def test_proxy_tool_materializes_rcm_inline_artifact(tmp_path) -> None:
    stdout = b"\x00remote\xff\n"
    stderr = b"warning\n"

    class ArtifactClient:
        async def call_tool_mcp(self, name: str, arguments: dict) -> CallToolResult:
            return CallToolResult(
                content=[],
                structuredContent={
                    "artifact_protocol": "rcm-inline-base64-v1",
                    "run_id": "remote-run",
                    "returncode": 0,
                    "timed_out": False,
                    "stdout_bytes": len(stdout),
                    "stderr_bytes": len(stderr),
                    "stdout_base64": base64.b64encode(stdout).decode("ascii"),
                    "stderr_base64": base64.b64encode(stderr).decode("ascii"),
                },
            )

    store = Store(tmp_path / "runs", (tmp_path / "runs").as_uri(), local_urls=True)
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
    )

    result = await tool.run({})
    data = result.structured_content
    assert data["run_id"] != "remote-run"
    assert data["stdout_url"].startswith("file://")
    assert data["stderr_url"].startswith("file://")
    assert "stdout_base64" not in data
    assert "stderr_base64" not in data
    assert "artifact_protocol" not in data
    assert Path(urlparse(data["stdout_url"]).path).read_bytes() == stdout
    assert Path(urlparse(data["stderr_url"]).path).read_bytes() == stderr


@pytest.mark.asyncio
async def test_remote_http_config_resolves_without_stdio_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rcm.proxy as proxy_module

    target = ProxyTargetSpec(
        name="remote",
        transport="remote",
        ssh=SSHSpec(host="compile-machine"),
        remote_config=RemoteConfigSpec(path="/etc/rcm/commands.yaml"),
        headers={"X-Local": HeaderSpec(value="local")},
    )

    async def fake_read(_: ProxyTargetSpec) -> RemoteServerMetadata:
        return RemoteServerMetadata(
            transport="http",
            public_base_url="https://compile.example.com/rcm/",
            api_key="remote-key",
            cwd="/srv/project",
        )

    async def unexpected_discovery(_: str) -> list[str]:
        raise AssertionError("HTTP remote configs must not discover or start rcm")

    monkeypatch.setattr(proxy_module, "_read_remote_metadata", fake_read)
    monkeypatch.setattr(
        proxy_module, "_discover_remote_stdio_command", unexpected_discovery
    )
    monkeypatch.chdir(tmp_path)

    resolved = await _resolve_remote_target(target)

    assert resolved.transport == "http"
    assert resolved.endpoint == "https://compile.example.com/rcm/mcp"
    assert resolved.remote_config is None
    assert resolved.ssh is not None and resolved.ssh.command is None
    assert resolved.headers["Authorization"].value == "Bearer remote-key"
    assert resolved.headers["X-Local"].value == "local"
    assert resolved.sync is not None
    assert resolved.sync.source == str(tmp_path)
    assert resolved.sync.destination == "/srv/project"
    assert resolved.sync.excludes == []
    assert resolved.sync.delete is False


@pytest.mark.asyncio
async def test_remote_config_metadata_uses_explicit_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy as proxy_module

    target = ProxyTargetSpec(
        name="remote",
        transport="remote",
        ssh=SSHSpec(host="compile-machine"),
        remote_config=RemoteConfigSpec(path="/etc/rcm/commands.yaml"),
    )

    async def fake_ssh(_: str, command: str) -> tuple[int, str, str]:
        assert command == "cat -- /etc/rcm/commands.yaml"
        return (
            0,
            """
            server:
              transport: http
              public_base_url: https://compile.example.com
            auth:
              api_key: remote-key
            defaults:
              cwd: /srv/project
            """,
            "",
        )

    monkeypatch.setattr(proxy_module, "_ssh_command", fake_ssh)

    metadata = await _read_remote_metadata(target)

    assert metadata.transport == "http"
    assert metadata.public_base_url == "https://compile.example.com"
    assert metadata.api_key == "remote-key"
    assert metadata.cwd == "/srv/project"


@pytest.mark.asyncio
async def test_remote_stdio_discovery_falls_back_to_uvx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy as proxy_module

    calls: list[str] = []

    async def fake_ssh(_: str, command: str) -> tuple[int, str, str]:
        calls.append(command)
        if command == "command -v rcm":
            return 1, "", "rcm not found"
        return 0, "/home/me/.local/bin/uvx\n", ""

    monkeypatch.setattr(proxy_module, "_ssh_command", fake_ssh)

    command = await _discover_remote_stdio_command("compile-machine")

    assert command == ["/home/me/.local/bin/uvx", "rcm", "--stdio"]
    assert calls == ["command -v rcm", "command -v uvx"]


@pytest.mark.asyncio
async def test_remote_stdio_config_discovers_rcm_and_keeps_remote_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rcm.proxy as proxy_module

    target = ProxyTargetSpec(
        name="remote",
        transport="remote",
        ssh=SSHSpec(host="compile-machine"),
        remote_config=RemoteConfigSpec(path="/etc/rcm/commands.yaml"),
    )

    async def fake_read(_: ProxyTargetSpec) -> RemoteServerMetadata:
        return RemoteServerMetadata(transport="stdio", cwd="/srv/project")

    async def fake_discovery(_: str) -> list[str]:
        return ["/opt/rcm/bin/rcm", "--stdio"]

    monkeypatch.setattr(proxy_module, "_read_remote_metadata", fake_read)
    monkeypatch.setattr(
        proxy_module, "_discover_remote_stdio_command", fake_discovery
    )
    monkeypatch.chdir(tmp_path)

    resolved = await _resolve_remote_target(target)

    assert resolved.transport == "ssh"
    assert resolved.ssh is not None
    assert resolved.ssh.command == ["/opt/rcm/bin/rcm", "--stdio"]
    assert resolved.remote_config is not None
    assert resolved.remote_config.path == "/etc/rcm/commands.yaml"
    assert resolved.sync is not None
    assert resolved.sync.source == str(tmp_path)
    assert resolved.sync.destination == "/srv/project"


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
        commands=[
            CommandSpec(
                name="local_echo",
                description="Echo locally.",
                command=[sys.executable, "-c", "print('local')"],
            )
        ],
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
        assert {tool.name for tool in tools} == {"local_echo", "compile__remote_echo"}
        tool = await mcp.get_tool("compile__remote_echo")
        assert tool is not None
        result = await tool.run({"value": "ok"})
        assert result.content[0].text == "ok"
        local_tool = await mcp.get_tool("local_echo")
        assert local_tool is not None
        local_result = await local_tool.run({})
        assert local_result.structured_content["stdout_bytes"] > 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_proxy_runtime_recovers_remote_rcm_artifact(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    remote_config = tmp_path / "remote-commands.yaml"
    code = (
        "import sys; sys.stdout.buffer.write(bytes([0, 255])); "
        "sys.stderr.buffer.write(b'err')"
    )
    remote_config.write_text(
        f"commands:\n"
        f"  - name: remote_binary\n"
        f"    description: Remote binary output.\n"
        f"    command: [{sys.executable!r}, '-c', {code!r}]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RCM_CONFIG", str(remote_config))
    monkeypatch.setenv("RCM_RUNS_DIR", str(tmp_path / "remote-runs"))

    cfg = Config(
        server=ServerSpec(),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[],
        proxy=ProxySpec(
            targets=[
                ProxyTargetSpec(
                    name="remote",
                    transport="stdio",
                    command=[sys.executable, "-m", "rcm", "--stdio"],
                )
            ]
        ),
    )
    store = Store(tmp_path / "local-runs", (tmp_path / "local-runs").as_uri(), local_urls=True)
    mcp, runtime = await build_proxy_server(cfg, store, None)
    try:
        tool = await mcp.get_tool("remote__remote_binary")
        assert tool is not None
        result = await tool.run({})
        data = result.structured_content
        assert data["stdout_url"].startswith("file://")
        assert data["stderr_url"].startswith("file://")
        assert Path(urlparse(data["stdout_url"]).path).read_bytes() == bytes([0, 255])
        assert Path(urlparse(data["stderr_url"]).path).read_bytes() == b"err"
        assert "stdout_base64" not in data
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
