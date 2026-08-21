"""MCP proxy target management and active-sync tool wrappers."""

from __future__ import annotations

import os
import shutil
from contextlib import AsyncExitStack
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import (
    SSETransport,
    StdioTransport,
    StreamableHttpTransport,
)
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import Tool, ToolResult
from pydantic import PrivateAttr

from .auth import ApiKeyAuth
from .config import Config, ProxyTargetSpec
from .sync import SyncError, SyncRunner


class ProxyError(RuntimeError):
    """Raised when a proxy target cannot be prepared."""


def _resolve_headers(
    target: ProxyTargetSpec,
) -> dict[str, str]:
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


def _mcp_proxy_command() -> tuple[str, list[str]]:
    executable = shutil.which("mcp-proxy")
    if executable is not None:
        return executable, []
    raise ProxyError(
        "mcp-proxy executable was not found; install the mcp-proxy dependency "
        "or use the native MCP HTTP/SSE fallback"
    )


def _build_transport(target: ProxyTargetSpec):
    if target.transport == "stdio":
        assert target.command is not None
        return StdioTransport(
            command=target.command[0],
            args=target.command[1:],
            cwd=target.cwd,
        )

    if target.transport == "ssh":
        assert target.ssh is not None
        return StdioTransport(
            command="ssh",
            args=[target.ssh.host, *target.ssh.command],
        )

    assert target.endpoint is not None
    headers = _resolve_headers(target)
    if target.transport == "http":
        proxy_transport = "streamablehttp"
    else:
        proxy_transport = "sse"

    try:
        command, prefix = _mcp_proxy_command()
    except ProxyError:
        # Keep the compiled binary usable when its environment does not ship
        # the optional console-script wrapper. The MCP SDK still provides the
        # same two client transports.
        if target.transport == "http":
            return StreamableHttpTransport(target.endpoint, headers=headers)
        return SSETransport(target.endpoint, headers=headers)

    args = [*prefix, "--transport", proxy_transport]
    for name, value in headers.items():
        args.extend(["--headers", name, value])
    args.append(target.endpoint)
    return StdioTransport(command=command, args=args)


class ProxyTool(Tool):
    """A FastMCP tool that syncs and forwards one remote MCP tool call."""

    _target_name: str = PrivateAttr()
    _remote_name: str = PrivateAttr()
    _client: Client = PrivateAttr()
    _sync_runner: SyncRunner | None = PrivateAttr()

    def __init__(
        self,
        *,
        public_name: str,
        target_name: str,
        remote_name: str,
        description: str | None,
        parameters: dict[str, Any],
        output_schema: dict[str, Any] | None,
        client: Client,
        sync: SyncRunner | None,
    ) -> None:
        super().__init__(
            name=public_name,
            description=description or f"Proxy for {target_name}::{remote_name}",
            parameters=parameters,
            output_schema=output_schema,
        )
        self._target_name = target_name
        self._remote_name = remote_name
        self._client = client
        self._sync_runner = sync

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        if self._sync_runner is not None:
            try:
                await self._sync_runner.sync()
            except SyncError as exc:
                raise ToolError(str(exc)) from exc

        try:
            result = await self._client.call_tool_mcp(
                self._remote_name,
                arguments or {},
            )
        except Exception as exc:
            raise ToolError(
                f"proxy target {self._target_name!r} tool "
                f"{self._remote_name!r} failed: {exc}"
            ) from exc

        return ToolResult(
            content=result.content,
            structured_content=getattr(result, "structured_content", None),
            meta=getattr(result, "meta", None),
            is_error=getattr(result, "is_error", False),
        )


class ProxyRuntime:
    """Own connected remote clients and the locally registered proxy tools."""

    def __init__(self, server: FastMCP, stack: AsyncExitStack) -> None:
        self.server = server
        self._stack = stack

    @classmethod
    async def create(
        cls,
        cfg: Config,
        api_key: str,
    ) -> ProxyRuntime:
        if cfg.proxy is None:
            raise ProxyError("proxy configuration is missing")

        server = FastMCP("rcm")
        server.add_middleware(ApiKeyAuth(api_key))
        stack = AsyncExitStack()
        try:
            for target in cfg.proxy.targets:
                client = await stack.enter_async_context(
                    Client(
                        _build_transport(target),
                        name=f"rcm-proxy-{target.name}",
                    )
                )
                remote_tools = await client.list_tools()
                sync_runner = (
                    SyncRunner(target, cfg.config_path)
                    if target.sync is not None
                    else None
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
                        )
                    )
        except Exception:
            await stack.aclose()
            raise
        return cls(server, stack)

    async def close(self) -> None:
        await self._stack.aclose()
