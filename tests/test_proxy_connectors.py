"""Tests for proxy transport connectors and tunnel monitoring."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from fastmcp.client.transports import StreamableHttpTransport

from rcm.config import ProxyTargetSpec, SSHSpec
from rcm.proxy import ProxyError
from rcm.proxy.connectors import ConnectorContext, _prepare_target
from rcm.proxy.remote import RemoteServerMetadata
from rcm.proxy.runtime import _monitor_tunneled_target
from rcm.tunnel import OriginLockedAsyncTransport


@pytest.mark.asyncio
async def test_tunnel_connector_uses_internal_endpoint_and_unix_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy.connectors as proxy_module

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
async def test_tunnel_monitor_requires_two_consecutive_ping_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy.runtime as proxy_module

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

