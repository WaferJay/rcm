"""Proxy runtime orchestration and target health monitoring."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable

from fastmcp import Client, FastMCP
from mcp.types import Implementation

from .. import __version__
from ..artifacts import (
    DEFAULT_ARTIFACT_MODE,
    RCM_CAPABILITY,
    RCM_EXPERIMENTAL_CAPABILITIES,
    RCM_PROTOCOL_VERSION,
)
from ..auth import ApiKeyAuth
from ..config import Config
from ..store import Store
from ..sync import SyncError, SyncRunner
from .connectors import ConnectorContext, _prepare_target
from .errors import ProxyError
from .remote import _resolve_remote_target_context
from .tool import ProxyTool


def _supports_rcm_v2(client: Client) -> bool:
    initialized = client.initialize_result
    if initialized is None:
        return False
    experimental = initialized.capabilities.experimental or {}
    capability = experimental.get(RCM_CAPABILITY)
    if not isinstance(capability, dict):
        return False
    versions = capability.get("versions")
    return isinstance(versions, list) and RCM_PROTOCOL_VERSION in versions


class ProxyRuntime:
    """Own connected remote clients and the locally registered proxy tools."""

    def __init__(
        self,
        server: FastMCP,
        stack: AsyncExitStack,
        failure: asyncio.Future[ProxyError],
        monitor_tasks: list[asyncio.Task[None]],
    ) -> None:
        self.server = server
        self._stack = stack
        self._failure = failure
        self._monitor_tasks = monitor_tasks

    @classmethod
    async def create(
        cls,
        cfg: Config,
        api_key: str | None,
        store: Store,
    ) -> ProxyRuntime:
        if cfg.proxy is None:
            raise ProxyError("proxy configuration is missing")

        server = FastMCP(
            "rcm",
            version=__version__,
            experimental_capabilities=RCM_EXPERIMENTAL_CAPABILITIES,
        )
        if api_key is not None:
            server.add_middleware(ApiKeyAuth(api_key))
        stack = AsyncExitStack()
        failure: asyncio.Future[ProxyError] = (
            asyncio.get_running_loop().create_future()
        )
        monitor_tasks: list[asyncio.Task[None]] = []

        def report_failure(target_name: str, exc: Exception) -> None:
            if failure.done():
                return
            failure.set_result(
                ProxyError(
                    f"proxy target {target_name!r} connection failed: {exc}"
                )
            )

        try:
            for configured_target in cfg.proxy.targets:
                metadata = None
                if configured_target.remote_config is not None:
                    target, metadata = await _resolve_remote_target_context(
                        configured_target
                    )
                else:
                    target = configured_target
                if (
                    target.artifacts == "passthrough"
                    and target.transport != "http"
                ):
                    raise ProxyError(
                        f"proxy target {target.name!r}: artifact passthrough "
                        "requires an HTTP RCM target"
                    )
                try:
                    sync_runner = (
                        SyncRunner(
                            target,
                            cfg.config_path,
                            remote_config_path=(
                                configured_target.remote_config.path
                                if configured_target.remote_config is not None
                                else None
                            ),
                        )
                        if target.sync is not None and target.sync.enabled
                        else None
                    )
                except SyncError as exc:
                    raise ProxyError(
                        f"invalid sync configuration for target {target.name!r}: {exc}"
                    ) from exc
                prepared = await _prepare_target(
                    ConnectorContext(target=target, metadata=metadata),
                    stack,
                )
                try:
                    client = await stack.enter_async_context(
                        Client(
                            prepared.transport,
                            name=f"rcm-proxy-{target.name}",
                            client_info=Implementation(
                                name="rcm",
                                version=__version__,
                            ),
                        )
                    )
                except Exception as exc:
                    raise ProxyError(
                        f"proxy target {target.name!r} failed to connect: {exc}"
                    ) from exc
                rcm_peer = _supports_rcm_v2(client)
                if configured_target.remote_config is not None and not rcm_peer:
                    raise ProxyError(
                        f"remote-config target {target.name!r} does not support "
                        "the RCM artifact protocol v2"
                    )
                if target.artifacts is not None and not rcm_peer:
                    raise ProxyError(
                        f"proxy target {target.name!r} configures artifacts but "
                        "the target is not an RCM v2 server"
                    )
                artifact_mode = target.artifacts or DEFAULT_ARTIFACT_MODE
                try:
                    remote_tools = await client.list_tools()
                except Exception as exc:
                    raise ProxyError(
                        f"proxy target {target.name!r} failed to discover tools: {exc}"
                    ) from exc
                failure_reporter = None
                if prepared.monitor is not None:
                    failure_reporter = lambda exc, name=target.name: report_failure(
                        name,
                        exc,
                    )
                for remote_tool in remote_tools:
                    remote_name = remote_tool.name
                    public_name = f"{target.name}__{remote_name}"
                    parameters = getattr(remote_tool, "inputSchema", None)
                    if not isinstance(parameters, dict):
                        parameters = {"type": "object", "properties": {}}
                    output_schema = getattr(remote_tool, "outputSchema", None)
                    if not isinstance(output_schema, dict):
                        output_schema = None
                    server.add_tool(
                        ProxyTool(
                            public_name=public_name,
                            target_name=target.name,
                            remote_name=remote_name,
                            description=getattr(remote_tool, "description", None),
                            parameters=parameters,
                            output_schema=output_schema,
                            client=client,
                            sync=sync_runner,
                            store=store,
                            target_transport=target.transport,
                            ssh_host=(
                                target.ssh.host if target.ssh is not None else None
                            ),
                            artifact_mode=artifact_mode,
                            rcm_peer=rcm_peer,
                            artifact_fetcher=prepared.artifact_fetcher,
                            failure_reporter=failure_reporter,
                        )
                    )
                if prepared.monitor is not None:
                    monitor_tasks.append(
                        asyncio.create_task(
                            _run_target_monitor(
                                target.name,
                                client,
                                prepared.monitor,
                                report_failure,
                            )
                        )
                    )
        except BaseException:
            await _cancel_tasks(monitor_tasks)
            await stack.aclose()
            raise
        return cls(server, stack, failure, monitor_tasks)

    async def wait_failure(self) -> ProxyError:
        """Wait until a monitored target requires the proxy to terminate."""
        return await asyncio.shield(self._failure)

    async def close(self) -> None:
        await _cancel_tasks(self._monitor_tasks)
        self._monitor_tasks.clear()
        await self._stack.aclose()


TUNNEL_PING_INTERVAL = 30.0
TUNNEL_PING_TIMEOUT = 10.0
TUNNEL_PING_FAILURE_LIMIT = 2


async def _cancel_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _monitor_tunneled_target(
    client: Client,
    wait_for_tunnel: Callable[[], Awaitable[None]],
) -> None:
    tunnel_task = asyncio.create_task(wait_for_tunnel())
    ping_failures = 0
    try:
        while True:
            done, _ = await asyncio.wait(
                {tunnel_task},
                timeout=TUNNEL_PING_INTERVAL,
            )
            if tunnel_task in done:
                await tunnel_task
                raise ProxyError("SSH tunnel monitor stopped unexpectedly")
            try:
                healthy = await asyncio.wait_for(
                    client.ping(),
                    timeout=TUNNEL_PING_TIMEOUT,
                )
                if not healthy:
                    raise ProxyError("remote MCP ping returned an unhealthy response")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                ping_failures += 1
                if ping_failures >= TUNNEL_PING_FAILURE_LIMIT:
                    raise ProxyError(
                        f"remote MCP ping failed {ping_failures} consecutive "
                        f"times: {exc}"
                    ) from exc
            else:
                ping_failures = 0
    finally:
        if not tunnel_task.done():
            tunnel_task.cancel()
        await asyncio.gather(tunnel_task, return_exceptions=True)


async def _run_target_monitor(
    target_name: str,
    client: Client,
    wait_for_tunnel: Callable[[], Awaitable[None]],
    reporter: Callable[[str, Exception], None],
) -> None:
    try:
        await _monitor_tunneled_target(client, wait_for_tunnel)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reporter(target_name, exc)
