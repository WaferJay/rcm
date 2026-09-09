"""Tests for remote RCM discovery and target resolution."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import rcm.proxy.remote as proxy_module
from rcm.config import (
    HeaderSpec,
    ProxyTargetSpec,
    RemoteConfigSpec,
    SSHSpec,
    SyncMappingSpec,
    SyncSpec,
)
from rcm.proxy import ProxyError
from rcm.proxy.remote import (
    RemoteServerMetadata,
    _discover_remote_stdio_command,
    _read_remote_metadata,
    _remote_sync_spec,
    _resolve_remote_target,
    _ssh_command,
)


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
async def test_remote_http_config_resolves_without_stdio_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import rcm.proxy.remote as proxy_module

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
    import rcm.proxy.remote as proxy_module

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
async def test_remote_stdio_config_rejects_ssh_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy.remote as proxy_module

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
    import rcm.proxy.remote as proxy_module

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
    import rcm.proxy.remote as proxy_module

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
    import rcm.proxy.remote as proxy_module

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
async def test_remote_stdio_discovery_falls_back_to_uvx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy.remote as proxy_module

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
    import rcm.proxy.remote as proxy_module

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


def remote_target() -> ProxyTargetSpec:
    return ProxyTargetSpec(
        name="remote",
        transport="remote",
        ssh=SSHSpec(host="compile-machine"),
        remote_config=RemoteConfigSpec(path="/etc/rcm/commands.yaml"),
    )


@pytest.mark.parametrize(
    "document, expected",
    [
        (
            "- not\n- a\n- mapping\n",
            "remote config '/etc/rcm/commands.yaml' must contain a mapping",
        ),
        (
            "server: {transport: websocket}\n",
            "remote config '/etc/rcm/commands.yaml': server.transport must be "
            "`http` or `stdio`, got 'websocket'",
        ),
        (
            "server: {public_base_url: 'https://example.com'}\nauth: [invalid]\n",
            "remote config '/etc/rcm/commands.yaml': auth must be a mapping",
        ),
        (
            "server: {public_base_url: 'https://example.com'}\ndefaults: [invalid]\n",
            "remote config '/etc/rcm/commands.yaml': defaults must be a mapping",
        ),
    ],
)
@pytest.mark.asyncio
async def test_remote_runtime_errors_keep_their_context(
    document: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ssh(_: str, command: str) -> tuple[int, str, str]:
        assert command == "cat -- /etc/rcm/commands.yaml"
        return 0, document, ""

    monkeypatch.setattr(proxy_module, "_ssh_command", fake_ssh)

    with pytest.raises(ProxyError) as exc_info:
        await _read_remote_metadata(remote_target())
    assert str(exc_info.value) == expected


@pytest.mark.asyncio
async def test_remote_invalid_yaml_keeps_the_existing_error_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ssh(_: str, command: str) -> tuple[int, str, str]:
        return 0, "server: [", ""

    monkeypatch.setattr(proxy_module, "_ssh_command", fake_ssh)

    with pytest.raises(
        ProxyError,
        match=r"^invalid YAML in remote config '/etc/rcm/commands.yaml':",
    ):
        await _read_remote_metadata(remote_target())
