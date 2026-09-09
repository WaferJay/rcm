"""Tests for origin-locked HTTP over OpenSSH Unix-socket tunnels."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

from rcm.tunnel import (
    ArtifactRoute,
    HttpOrigin,
    OriginLockedAsyncTransport,
    RemoteHttpBinding,
    SSHTunnel,
    TunnelError,
    TunnelRouteError,
    build_ssh_tunnel_command,
    derive_remote_http_binding,
)


@pytest.mark.parametrize(
    "host, expected",
    [
        ("0.0.0.0", "127.0.0.1"),
        ("::", "::1"),
        ("[::]", "::1"),
        ("127.0.0.2", "127.0.0.2"),
        ("rcm.internal", "rcm.internal"),
    ],
)
def test_derive_remote_http_binding_normalizes_wildcards(
    host: str,
    expected: str,
) -> None:
    binding = derive_remote_http_binding(host, 8443, tls_enabled=True)

    assert binding == RemoteHttpBinding("https", expected, 8443)


@pytest.mark.parametrize("port", [True, 0, 65536, "8000"])
def test_derive_remote_http_binding_rejects_invalid_port(port) -> None:
    with pytest.raises(TunnelError, match="port"):
        derive_remote_http_binding("127.0.0.1", port, tls_enabled=False)


@pytest.mark.parametrize(
    "host",
    ["", "bad host", "host/path", "host:port", "[broken"],
)
def test_derive_remote_http_binding_rejects_invalid_host(host: str) -> None:
    with pytest.raises(TunnelError, match="host"):
        derive_remote_http_binding(host, 8000, tls_enabled=False)


def test_derive_remote_http_binding_rejects_non_boolean_tls() -> None:
    with pytest.raises(TunnelError, match="TLS"):
        derive_remote_http_binding("127.0.0.1", 8000, tls_enabled="yes")


def test_artifact_route_separates_public_and_internal_addresses() -> None:
    binding = derive_remote_http_binding("0.0.0.0", 8000, tls_enabled=False)
    route = ArtifactRoute.create("https://public.example/rcm/", binding)

    assert route.mcp_endpoint == "http://127.0.0.1:8000/mcp"
    assert route.tls_server_name == "public.example"
    assert route.artifact_url(
        "https://public.example/rcm/runs/remote-run/stdout",
        run_id="remote-run",
        artifact="stdout",
    ) == "http://127.0.0.1:8000/runs/remote-run/stdout"


def test_artifact_route_rejects_unexpected_public_url() -> None:
    route = ArtifactRoute.create(
        "https://public.example/rcm",
        RemoteHttpBinding("http", "127.0.0.1", 8000),
    )

    with pytest.raises(TunnelError, match="must match"):
        route.artifact_url(
            "https://attacker.example/runs/remote-run/stdout",
            run_id="remote-run",
            artifact="stdout",
        )


@pytest.mark.parametrize(
    "public_base_url",
    [
        "relative/path",
        "file:///tmp/rcm",
        "https://user@example.test/rcm",
        "https://example.test/rcm?source=external",
        "https://[invalid/rcm",
    ],
)
def test_artifact_route_rejects_invalid_public_base_url(
    public_base_url: str,
) -> None:
    with pytest.raises(TunnelError, match="public_base_url"):
        ArtifactRoute.create(
            public_base_url,
            RemoteHttpBinding("http", "127.0.0.1", 8000),
        )


@pytest.mark.asyncio
async def test_origin_lock_rejects_public_network_before_transport() -> None:
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    transport = OriginLockedAsyncTransport(
        HttpOrigin("http", "127.0.0.1", 8123),
        Path("/unused/tunnel.sock"),
    )
    await transport._delegate.aclose()
    transport._delegate = httpx.MockTransport(handle)
    try:
        with pytest.raises(TunnelRouteError):
            await transport.handle_async_request(
                httpx.Request("GET", "https://public.example/healthz")
            )
    finally:
        await transport.aclose()

    assert requests == []


@pytest.mark.asyncio
async def test_origin_lock_uses_public_hostname_for_internal_tls() -> None:
    server_names: list[str | None] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        server_names.append(request.extensions.get("sni_hostname"))
        return httpx.Response(200, request=request)

    transport = OriginLockedAsyncTransport(
        HttpOrigin("https", "127.0.0.1", 8443),
        Path("/unused/tunnel.sock"),
        tls_server_name="public.example",
    )
    await transport._delegate.aclose()
    transport._delegate = httpx.MockTransport(handle)
    try:
        response = await transport.handle_async_request(
            httpx.Request("GET", "https://127.0.0.1:8443/healthz")
        )
        assert response.status_code == 200
    finally:
        await transport.aclose()

    assert server_names == ["public.example"]


def test_ssh_tunnel_command_is_noninteractive_and_origin_scoped(tmp_path: Path) -> None:
    command = build_ssh_tunnel_command(
        "compile-machine",
        tmp_path / "http.sock",
        RemoteHttpBinding("http", "::1", 8000),
    )

    assert command[:3] == ["ssh", "-N", "-T"]
    assert "BatchMode=yes" in command
    assert "ExitOnForwardFailure=yes" in command
    assert command[-3:] == [
        "-L",
        f"{tmp_path}/http.sock:[::1]:8000",
        "compile-machine",
    ]


@pytest.mark.asyncio
async def test_origin_locked_transport_uses_unix_socket() -> None:
    socket_dir = tempfile.TemporaryDirectory(prefix="rcm-test-", dir="/tmp")
    socket_path = Path(socket_dir.name) / "http.sock"

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        assert b"Host: 127.0.0.1:8123" in request
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    try:
        server = await asyncio.start_unix_server(handle, path=socket_path)
    except PermissionError:
        socket_dir.cleanup()
        pytest.skip("sandbox does not permit local Unix sockets")
    try:
        async with httpx.AsyncClient(
            transport=OriginLockedAsyncTransport(
                HttpOrigin("http", "127.0.0.1", 8123),
                socket_path,
            )
        ) as client:
            response = await client.get("http://127.0.0.1:8123/status")
            assert response.text == "ok"
            with pytest.raises(TunnelRouteError):
                await client.get("http://other.example/status")
    finally:
        server.close()
        await server.wait_closed()
        socket_dir.cleanup()


@pytest.mark.asyncio
async def test_ssh_tunnel_cleans_up_process_and_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tunnel_dir = tmp_path / "tunnel"

    def fake_mkdtemp(*, prefix: str, dir: str | None) -> str:
        tunnel_dir.mkdir()
        return str(tunnel_dir)

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()
            self._stopped = asyncio.Event()

        async def wait(self) -> int:
            await self._stopped.wait()
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0
            self._stopped.set()

        def kill(self) -> None:
            self.returncode = -9
            self._stopped.set()

    process = FakeProcess()

    async def fake_subprocess(*args, **kwargs):
        assert kwargs["stdin"] is asyncio.subprocess.DEVNULL
        assert kwargs["stdout"] is asyncio.subprocess.DEVNULL
        Path(args[-2].split(":", 1)[0]).touch()
        return process

    monkeypatch.setattr("rcm.tunnel.tempfile.mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    tunnel = SSHTunnel(
        "compile-machine",
        RemoteHttpBinding("http", "127.0.0.1", 8000),
    )
    async with tunnel:
        assert tunnel.socket_path is not None
        assert tunnel.socket_path.exists()

    assert process.returncode == 0
    assert not tunnel_dir.exists()
