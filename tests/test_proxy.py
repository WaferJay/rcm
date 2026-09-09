"""Tests for proxied MCP tool forwarding."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import socket
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlparse

import pytest
import httpx
from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from mcp.types import CallToolResult, TextContent

from rcm.config import (
    AuthSpec,
    CollectPathSpec,
    CollectSpec,
    CommandSpec,
    Config,
    DefaultsSpec,
    HeaderSpec,
    ProxySpec,
    ProxyTargetSpec,
    RemoteConfigSpec,
    SSHSpec,
    ServerSpec,
    SyncMappingSpec,
    SyncSpec,
)
from rcm.artifacts import ArtifactDescriptor
from rcm.proxy import (
    ConnectorContext,
    ProxyError,
    ProxyTool,
    RemoteServerMetadata,
    _discover_remote_stdio_command,
    _copy_http_file,
    _copy_ssh_file,
    _file_path_from_uri,
    _monitor_tunneled_target,
    _prepare_target,
    _read_remote_metadata,
    _remote_sync_spec,
    _resolve_remote_target,
    _ssh_command,
)
from rcm.server import build_proxy_server, build_server
from rcm.store import Store
from rcm.sync import SyncError
from rcm.tunnel import OriginLockedAsyncTransport


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
async def test_ssh_command_does_not_inherit_mcp_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"remote output", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await _ssh_command("compile-machine", "cat -- /etc/rcm/config.yaml")

    assert result == (0, "remote output", "")
    assert captured["args"] == (
        "ssh",
        "compile-machine",
        "cat -- /etc/rcm/config.yaml",
    )
    assert captured["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL


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
async def test_http_artifact_downloads_without_mcp_authorization(tmp_path) -> None:
    payload = b"remote over http\x00"

    def handle(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, content=payload)

    descriptor = ArtifactDescriptor(
        uri="https://remote.example/runs/id/stdout",
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    destination = tmp_path / "stdout.log"

    await _copy_http_file(
        destination, descriptor, transport=httpx.MockTransport(handle)
    )

    assert destination.read_bytes() == payload


@pytest.mark.parametrize(
    "uri",
    [
        "file://other-host/var/run/stdout.log",
        "file:relative.log",
        "file:///var/run/stdout.log?tail=1",
        "file:///var/run/bad%ZZ.log",
        "file:///var/run/nul%00.log",
    ],
)
def test_file_artifact_uri_validation(uri: str) -> None:
    with pytest.raises(Exception):
        _file_path_from_uri(uri)


@pytest.mark.asyncio
async def test_ssh_artifact_streams_binary_output_separately_from_diagnostics(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"\x00remote\xff"
    captured: dict = {}

    class FakeProcess:
        returncode = 0

        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(payload)
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"ssh diagnostic that is not artifact data")
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    descriptor = ArtifactDescriptor(
        uri="file:///srv/rcm/run%20files/stdout.log",
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    destination = tmp_path / "stdout.log"

    await _copy_ssh_file(
        "compile-machine",
        _file_path_from_uri(descriptor.uri),
        destination,
        descriptor,
    )

    assert destination.read_bytes() == payload
    assert captured["args"] == (
        "ssh",
        "compile-machine",
        "cat -- '/srv/rcm/run files/stdout.log'",
    )
    assert captured["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL


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


@pytest.mark.asyncio
async def test_remote_http_config_resolves_without_stdio_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import rcm.proxy as proxy_module

    target = ProxyTargetSpec(
        name="remote",
        transport="remote",
        ssh=SSHSpec(host="compile-machine"),
        remote_config=RemoteConfigSpec(path="/etc/rcm/commands.yaml"),
        headers={"X-Local": HeaderSpec(value="local")},
        artifacts="passthrough",
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
    assert resolved.artifacts == "passthrough"
    assert resolved.sync is not None
    mapping = resolved.sync.mappings[0]
    assert mapping.source == str(tmp_path)
    assert mapping.destination == "/srv/project"
    assert mapping.excludes == []
    assert mapping.delete is False
    assert "implicit full-directory sync" in caplog.text


@pytest.mark.asyncio
async def test_remote_http_tunnel_resolves_internal_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy as proxy_module

    target = ProxyTargetSpec(
        name="remote",
        transport="remote",
        ssh=SSHSpec(host="compile-machine", tunnel=True),
        remote_config=RemoteConfigSpec(path="/etc/rcm/commands.yaml"),
        sync=SyncSpec(enabled=False),
    )

    async def fake_read(_: ProxyTargetSpec) -> RemoteServerMetadata:
        return RemoteServerMetadata(
            transport="http",
            public_base_url="https://public.example/rcm",
            api_key="remote-key",
            host="0.0.0.0",
            port=8123,
            tls_enabled=False,
        )

    monkeypatch.setattr(proxy_module, "_read_remote_metadata", fake_read)
    monkeypatch.chdir(tmp_path)

    resolved = await _resolve_remote_target(target)

    assert resolved.transport == "http"
    assert resolved.endpoint == "http://127.0.0.1:8123/mcp"
    assert resolved.ssh is not None and resolved.ssh.tunnel is True
    assert resolved.headers["Authorization"].value == "Bearer remote-key"
    assert resolved.artifacts == "localize"


@pytest.mark.asyncio
async def test_tunnel_connector_uses_internal_endpoint_and_unix_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy as proxy_module

    tunnel_closed = False
    socket_path = tmp_path / "http.sock"

    class FakeTunnel:
        def __init__(self, ssh_host, binding) -> None:
            assert ssh_host == "compile-machine"
            assert binding.base_url == "http://127.0.0.1:8123"
            self.socket_path = socket_path

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            nonlocal tunnel_closed
            tunnel_closed = True

        async def wait(self) -> None:
            await asyncio.Future()

    monkeypatch.setattr(proxy_module, "SSHTunnel", FakeTunnel)
    target = ProxyTargetSpec(
        name="remote",
        transport="http",
        endpoint="http://127.0.0.1:8123/mcp",
        ssh=SSHSpec(host="compile-machine", tunnel=True),
    )
    metadata = RemoteServerMetadata(
        transport="http",
        public_base_url="https://public.example/rcm",
        host="0.0.0.0",
        port=8123,
    )

    async with AsyncExitStack() as stack:
        prepared = await _prepare_target(
            ConnectorContext(target=target, metadata=metadata),
            stack,
        )
        assert isinstance(prepared.transport, StreamableHttpTransport)
        assert prepared.transport.url == "http://127.0.0.1:8123/mcp"
        assert prepared.transport.httpx_client_factory is not None
        client = prepared.transport.httpx_client_factory()
        try:
            assert isinstance(client._transport, OriginLockedAsyncTransport)
        finally:
            await client.aclose()

    assert tunnel_closed is True


@pytest.mark.asyncio
async def test_proxy_runtime_wraps_initial_connection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy as proxy_module

    class FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            raise RuntimeError("connection refused")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            pass

    monkeypatch.setattr(proxy_module, "Client", FailingClient)
    cfg = Config(
        server=ServerSpec(),
        auth=AuthSpec(),
        defaults=DefaultsSpec(),
        commands=[],
        proxy=ProxySpec(
            targets=[
                ProxyTargetSpec(
                    name="remote",
                    transport="stdio",
                    command=["unused"],
                )
            ]
        ),
    )

    with pytest.raises(ProxyError, match="failed to connect: connection refused"):
        await build_proxy_server(
            cfg,
            Store(tmp_path / "runs", (tmp_path / "runs").as_uri()),
            None,
        )


@pytest.mark.asyncio
async def test_remote_stdio_config_rejects_ssh_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy as proxy_module

    target = ProxyTargetSpec(
        name="remote",
        transport="remote",
        ssh=SSHSpec(host="compile-machine", tunnel=True),
        remote_config=RemoteConfigSpec(path="/etc/rcm/commands.yaml"),
        sync=SyncSpec(enabled=False),
    )

    async def fake_read(_: ProxyTargetSpec) -> RemoteServerMetadata:
        return RemoteServerMetadata(transport="stdio")

    monkeypatch.setattr(proxy_module, "_read_remote_metadata", fake_read)

    with pytest.raises(ProxyError, match="requires.*HTTP"):
        await _resolve_remote_target(target)


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
async def test_remote_config_metadata_reads_tunnel_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy as proxy_module

    target = ProxyTargetSpec(
        name="remote",
        transport="remote",
        ssh=SSHSpec(host="compile-machine", tunnel=True),
        remote_config=RemoteConfigSpec(path="/etc/rcm/commands.yaml"),
    )

    async def fake_ssh(_: str, command: str) -> tuple[int, str, str]:
        return (
            0,
            """
            server:
              transport: http
              host: '::'
              port: 8443
              public_base_url: https://public.example/rcm
              tls:
                enabled: true
            """,
            "",
        )

    monkeypatch.setattr(proxy_module, "_ssh_command", fake_ssh)

    metadata = await _read_remote_metadata(target)

    assert metadata.host == "::"
    assert metadata.port == 8443
    assert metadata.tls_enabled is True


@pytest.mark.parametrize(
    "listener_yaml, fragment",
    [
        ("host: ''", "server.host"),
        ("port: true", "server.port"),
        ("port: 65536", "server.port"),
        ("tls: []", "server.tls"),
        ("tls: {enabled: yes-please}", "server.tls.enabled"),
    ],
)
@pytest.mark.asyncio
async def test_remote_config_metadata_rejects_invalid_tunnel_listener(
    listener_yaml: str,
    fragment: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy as proxy_module

    target = ProxyTargetSpec(
        name="remote",
        transport="remote",
        ssh=SSHSpec(host="compile-machine", tunnel=True),
        remote_config=RemoteConfigSpec(path="/etc/rcm/commands.yaml"),
    )

    async def fake_ssh(_: str, command: str) -> tuple[int, str, str]:
        return (
            0,
            "server:\n"
            "  transport: http\n"
            "  public_base_url: https://public.example/rcm\n"
            f"  {listener_yaml}\n",
            "",
        )

    monkeypatch.setattr(proxy_module, "_ssh_command", fake_ssh)

    with pytest.raises(ProxyError) as exc_info:
        await _read_remote_metadata(target)
    assert fragment in str(exc_info.value)


@pytest.mark.asyncio
async def test_tunnel_monitor_requires_two_consecutive_ping_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy as proxy_module

    monkeypatch.setattr(proxy_module, "TUNNEL_PING_INTERVAL", 0.001)
    monkeypatch.setattr(proxy_module, "TUNNEL_PING_TIMEOUT", 0.1)

    class PingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def ping(self) -> bool:
            self.calls += 1
            if self.calls in {1, 3}:
                return False
            if self.calls == 4:
                raise RuntimeError("unhealthy")
            return True

    async def tunnel_wait() -> None:
        await asyncio.Future()

    client = PingClient()
    with pytest.raises(ProxyError, match="2 consecutive"):
        await _monitor_tunneled_target(client, tunnel_wait)
    assert client.calls == 4


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
    mapping = resolved.sync.mappings[0]
    assert mapping.source == str(tmp_path)
    assert mapping.destination == "/srv/project"


@pytest.mark.parametrize(
    "cwd, destination, expected",
    [
        ("/srv/project", "backend", "/srv/project/backend"),
        (None, "backend", "/etc/rcm/backend"),
        ("/srv/project", "/opt/backend", "/opt/backend"),
        ("/srv/project", "other-host:backend", "other-host:backend"),
    ],
)
def test_remote_sync_resolves_relative_destinations(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    cwd: str | None,
    destination: str,
    expected: str,
) -> None:
    remote_config = RemoteConfigSpec(path="/etc/rcm/commands.yaml")
    target = ProxyTargetSpec(
        name="remote",
        transport="remote",
        ssh=SSHSpec(host="compile-machine"),
        remote_config=remote_config,
        sync=SyncSpec(
            mappings=[
                SyncMappingSpec(source=str(tmp_path), destination=destination)
            ]
        ),
    )

    resolved = _remote_sync_spec(
        target,
        remote_config,
        RemoteServerMetadata(transport="stdio", cwd=cwd),
    )

    assert resolved is not None
    assert resolved.mappings[0].destination == expected
    assert "implicit full-directory sync" not in caplog.text


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
        assert local_result.structured_content["stdout"]["bytes"] > 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_explicit_artifact_mode_rejects_non_rcm_target(tmp_path) -> None:
    import sys

    remote_code = """
from fastmcp import FastMCP
mcp = FastMCP('ordinary-mcp')
@mcp.tool
def echo(value: str) -> str:
    return value
mcp.run()
"""
    cfg = Config(
        server=ServerSpec(),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[],
        proxy=ProxySpec(
            targets=[
                ProxyTargetSpec(
                    name="ordinary",
                    transport="stdio",
                    command=[sys.executable, "-c", remote_code],
                    artifacts="localize",
                )
            ]
        ),
    )

    with pytest.raises(ProxyError, match="target is not an RCM v2 server"):
        await build_proxy_server(
            cfg,
            Store(tmp_path / "runs", (tmp_path / "runs").as_uri()),
            None,
        )


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
        assert data["stdout"]["uri"].startswith("file://")
        assert data["stderr"]["uri"].startswith("file://")
        assert Path(urlparse(data["stdout"]["uri"]).path).read_bytes() == bytes([0, 255])
        assert Path(urlparse(data["stderr"]["uri"]).path).read_bytes() == b"err"
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


@pytest.mark.asyncio
async def test_http_rcm_target_localizes_or_passthroughs_artifacts(tmp_path) -> None:
    import sys

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base_url = f"http://127.0.0.1:{port}"
    remote_work = tmp_path / "remote-work"
    remote_work.mkdir()
    remote_cfg = Config(
        server=ServerSpec(public_base_url=base_url),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[
            CommandSpec(
                name="binary",
                description="Return binary output.",
                command=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path;import sys;Path('artifact.bin').write_bytes(b'artifact');sys.stdout.buffer.write(b'out\\x00');sys.stderr.buffer.write(b'err')",
                ],
                cwd=str(remote_work),
                collect=CollectSpec(paths=(CollectPathSpec("artifact.bin"),)),
            )
        ],
    )
    remote_store = Store(tmp_path / "remote-runs", base_url)
    remote = build_server(remote_cfg, remote_store, api_key=None)
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
                raise RuntimeError("remote HTTP RCM failed to start")
            await asyncio.sleep(0.05)

    async def call_through(mode: str | None, runs_name: str):
        target = ProxyTargetSpec(
            name="remote",
            transport="http",
            endpoint=f"{base_url}/mcp",
            artifacts=mode,
        )
        cfg = Config(
            server=ServerSpec(),
            auth=AuthSpec(api_key=None),
            defaults=DefaultsSpec(),
            commands=[],
            proxy=ProxySpec(targets=[target]),
        )
        local_store = Store(
            tmp_path / runs_name,
            (tmp_path / runs_name).as_uri(),
            local_urls=True,
        )
        mcp, runtime = await build_proxy_server(cfg, local_store, None)
        try:
            tool = await mcp.get_tool("remote__binary")
            assert tool is not None
            result = await tool.run({})
            return result.structured_content, local_store
        finally:
            await runtime.close()

    try:
        localized, local_store = await call_through(None, "localized-runs")
        assert localized["run_id"] not in {
            child.name for child in remote_store.runs_dir.iterdir()
        }
        assert localized["stdout"]["uri"].startswith("file://")
        assert Path(urlparse(localized["stdout"]["uri"]).path).read_bytes() == b"out\x00"
        assert Path(urlparse(localized["stderr"]["uri"]).path).read_bytes() == b"err"
        localized_collect = Path(urlparse(localized["collect"]["uri"]).path)
        assert localized_collect.name == "collect.tar.gz"
        assert localized_collect.read_bytes().startswith(b"\x1f\x8b")
        assert len(list(local_store.runs_dir.iterdir())) == 1

        passed, passthrough_store = await call_through(
            "passthrough", "passthrough-runs"
        )
        assert passed["stdout"]["uri"].startswith(f"{base_url}/runs/")
        assert passed["collect"]["uri"].startswith(f"{base_url}/runs/")
        assert passed["run_id"] in {
            child.name for child in remote_store.runs_dir.iterdir()
        }
        assert list(passthrough_store.runs_dir.iterdir()) == []
    finally:
        remote_task.cancel()
        try:
            await remote_task
        except BaseException:
            pass
