"""In-process tool runtime used by the command-line interface."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

from fastmcp import FastMCP
from fastmcp.tools.base import Tool, ToolResult

from .config import load_config
from .proxy import ProxyRuntime
from .server import build_proxy_server, build_server
from .store import Store


class ConfiguredToolRuntime:
    """Expose configured RCM tools without starting an MCP transport.

    The runtime owns any proxy connections it opens and must be used as an
    asynchronous context manager.  Keeping that lifecycle here lets CLI
    commands remain independent of the concrete local/proxy implementation.
    """

    def __init__(self, server: FastMCP, proxy_runtime: ProxyRuntime | None) -> None:
        self._server = server
        self._proxy_runtime = proxy_runtime

    @classmethod
    async def open(cls, config_path: str | Path) -> ConfiguredToolRuntime:
        """Load a config and construct its in-process tool provider."""
        cfg = load_config(config_path)
        runs_dir = Path(os.environ.get("RCM_RUNS_DIR", "./runs")).resolve()
        store = Store(
            runs_dir,
            public_base_url=runs_dir.as_uri(),
            local_urls=True,
        )

        retention_raw = os.environ.get("RCM_RUNS_RETENTION", "0")
        try:
            retention = int(retention_raw)
        except ValueError:
            retention = 0
        if retention > 0:
            store.prune(retention)

        if cfg.proxy is None:
            return cls(build_server(cfg, store, api_key=None), None)

        server, proxy_runtime = await build_proxy_server(cfg, store, api_key=None)
        return cls(server, proxy_runtime)

    async def __aenter__(self) -> ConfiguredToolRuntime:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def list_tools(self) -> Sequence[Tool]:
        """Return every enabled local and proxied tool."""
        return await self._server.list_tools()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> ToolResult:
        """Call one configured tool through FastMCP's public API."""
        result = await self._server.call_tool(name, arguments)
        if not isinstance(result, ToolResult):
            raise RuntimeError("task-based tool results are not supported by the CLI")
        return result

    async def close(self) -> None:
        """Close owned proxy connections; safe to call more than once."""
        if self._proxy_runtime is None:
            return
        proxy_runtime = self._proxy_runtime
        self._proxy_runtime = None
        await proxy_runtime.close()
