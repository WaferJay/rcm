"""Transport connectors for prepared proxy targets."""

from __future__ import annotations

import os
import shlex
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Protocol

from fastmcp.client.transports import (
    ClientTransport,
    SSETransport,
    StdioTransport,
    StreamableHttpTransport,
)

from ..config import ProxyTargetSpec
from ..tunnel import (
    ArtifactRoute,
    SSHTunnel,
    TunnelError,
    derive_remote_http_binding,
    uds_http_client_factory,
)
from .artifact_transfer import (
    ArtifactFetcher,
    SchemeArtifactFetcher,
    TunneledHttpArtifactFetcher,
    _standard_artifact_fetcher,
)
from .errors import ProxyError
from .remote import RemoteServerMetadata


def _resolve_headers(target: ProxyTargetSpec) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for name, spec in target.headers.items():
        if spec.value is not None:
            resolved[name] = spec.value
            continue
        if spec.env is None:
            raise ProxyError(f"header {name!r} for target {target.name!r} has no source")
        value = os.environ.get(spec.env)
        if not value:
            raise ProxyError(
                f"environment variable {spec.env!r} for target "
                f"{target.name!r} header {name!r} is missing or empty"
            )
        resolved[name] = value
    return resolved


@dataclass(frozen=True)
class ConnectorContext:
    target: ProxyTargetSpec
    metadata: RemoteServerMetadata | None = None


@dataclass(frozen=True)
class PreparedTarget:
    transport: ClientTransport
    artifact_fetcher: ArtifactFetcher
    monitor: Callable[[], Awaitable[None]] | None = None


class TargetConnector(Protocol):
    async def prepare(
        self,
        context: ConnectorContext,
        stack: AsyncExitStack,
    ) -> PreparedTarget: ...


class StdioConnector:
    async def prepare(
        self,
        context: ConnectorContext,
        stack: AsyncExitStack,
    ) -> PreparedTarget:
        target = context.target
        if not target.command:
            raise ProxyError(f"proxy target {target.name!r} has no stdio command")
        return PreparedTarget(
            transport=StdioTransport(
                command=target.command[0],
                args=target.command[1:],
                cwd=target.cwd,
                env=dict(os.environ),
            ),
            artifact_fetcher=_standard_artifact_fetcher("stdio"),
        )


class SSHStdioConnector:
    async def prepare(
        self,
        context: ConnectorContext,
        stack: AsyncExitStack,
    ) -> PreparedTarget:
        target = context.target
        if target.ssh is None or target.ssh.command is None:
            raise ProxyError(f"proxy target {target.name!r} has no SSH command")
        remote_args: list[str] = []
        if target.remote_config is not None:
            remote_args.extend(
                ["env", f"RCM_CONFIG={shlex.quote(target.remote_config.path)}"]
            )
        return PreparedTarget(
            transport=StdioTransport(
                command="ssh",
                args=[
                    target.ssh.host,
                    *remote_args,
                    *target.ssh.command,
                ],
            ),
            artifact_fetcher=_standard_artifact_fetcher(
                "ssh",
                target.ssh.host,
            ),
        )


class DirectHttpConnector:
    async def prepare(
        self,
        context: ConnectorContext,
        stack: AsyncExitStack,
    ) -> PreparedTarget:
        target = context.target
        if target.endpoint is None:
            raise ProxyError(f"proxy target {target.name!r} has no HTTP endpoint")
        return PreparedTarget(
            transport=StreamableHttpTransport(
                target.endpoint,
                headers=_resolve_headers(target),
            ),
            artifact_fetcher=_standard_artifact_fetcher("http"),
        )


class SSEConnector:
    async def prepare(
        self,
        context: ConnectorContext,
        stack: AsyncExitStack,
    ) -> PreparedTarget:
        target = context.target
        if target.endpoint is None:
            raise ProxyError(f"proxy target {target.name!r} has no SSE endpoint")
        return PreparedTarget(
            transport=SSETransport(
                target.endpoint,
                headers=_resolve_headers(target),
            ),
            artifact_fetcher=_standard_artifact_fetcher("sse"),
        )


class SSHTunnelHttpConnector:
    async def prepare(
        self,
        context: ConnectorContext,
        stack: AsyncExitStack,
    ) -> PreparedTarget:
        target = context.target
        metadata = context.metadata
        if target.ssh is None or not target.ssh.tunnel or metadata is None:
            raise ProxyError(f"proxy target {target.name!r} has no SSH tunnel metadata")
        if metadata.public_base_url is None:
            raise ProxyError(
                f"proxy target {target.name!r} has no remote public_base_url"
            )
        try:
            binding = derive_remote_http_binding(
                metadata.host,
                metadata.port,
                tls_enabled=metadata.tls_enabled,
            )
            route = ArtifactRoute.create(metadata.public_base_url, binding)
            tunnel = await stack.enter_async_context(
                SSHTunnel(target.ssh.host, binding)
            )
        except TunnelError as exc:
            raise ProxyError(
                f"proxy target {target.name!r}: failed to establish SSH tunnel: {exc}"
            ) from exc
        socket_path = tunnel.socket_path
        if socket_path is None:
            raise ProxyError(
                f"proxy target {target.name!r}: SSH tunnel has no socket path"
            )
        tunneled_fetcher = TunneledHttpArtifactFetcher(route, socket_path)
        return PreparedTarget(
            transport=StreamableHttpTransport(
                route.mcp_endpoint,
                headers=_resolve_headers(target),
                httpx_client_factory=uds_http_client_factory(
                    route.internal_origin,
                    socket_path,
                    tls_server_name=route.tls_server_name,
                ),
            ),
            artifact_fetcher=SchemeArtifactFetcher(
                {
                    "http": tunneled_fetcher,
                    "https": tunneled_fetcher,
                }
            ),
            monitor=tunnel.wait,
        )


TARGET_CONNECTORS: Mapping[str, TargetConnector] = {
    "stdio": StdioConnector(),
    "ssh": SSHStdioConnector(),
    "http": DirectHttpConnector(),
    "sse": SSEConnector(),
    "ssh-tunnel-http": SSHTunnelHttpConnector(),
}


async def _prepare_target(
    context: ConnectorContext,
    stack: AsyncExitStack,
) -> PreparedTarget:
    target = context.target
    key = (
        "ssh-tunnel-http"
        if target.transport == "http"
        and target.ssh is not None
        and target.ssh.tunnel
        else target.transport
    )
    connector = TARGET_CONNECTORS.get(key)
    if connector is None:
        raise ProxyError(
            f"proxy target {target.name!r} uses unsupported connector {key!r}"
        )
    return await connector.prepare(context, stack)
