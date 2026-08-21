"""MCP proxy target management and active-sync tool wrappers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import shlex
import shutil
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

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
from .artifacts import ARTIFACT_ENV, ARTIFACT_ENV_VALUE, ARTIFACT_PROTOCOL
from .config import (
    Config,
    HeaderSpec,
    ProxyTargetSpec,
    RemoteConfigSpec,
    SyncSpec,
)
from .store import Store
from .sync import SyncError, SyncRunner


class ProxyError(RuntimeError):
    """Raised when a proxy target cannot be prepared."""


@dataclass(frozen=True)
class RemoteServerMetadata:
    transport: str
    public_base_url: str | None = None
    api_key: str | None = None
    cwd: str | None = None


async def _ssh_command(host: str, command: str) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            host,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ProxyError(f"failed to execute ssh for remote host {host!r}: {exc}") from exc
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


async def _read_remote_metadata(target: ProxyTargetSpec) -> RemoteServerMetadata:
    if target.ssh is None or target.remote_config is None:
        raise ProxyError(f"proxy target {target.name!r} is missing remote SSH config")
    path = target.remote_config.path
    returncode, stdout, stderr = await _ssh_command(
        target.ssh.host,
        f"cat -- {shlex.quote(path)}",
    )
    if returncode != 0:
        detail = stderr.strip() or stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise ProxyError(
            f"failed to read remote config {path!r} on host {target.ssh.host!r}"
            f" (exit code {returncode}){suffix}"
        )

    try:
        raw = yaml.safe_load(stdout)
    except yaml.YAMLError as exc:
        raise ProxyError(f"invalid YAML in remote config {path!r}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProxyError(f"remote config {path!r} must contain a mapping")

    server_raw = raw.get("server") or {}
    if not isinstance(server_raw, dict):
        raise ProxyError(f"remote config {path!r}: server must be a mapping")
    transport = server_raw.get("transport", "http")
    if not isinstance(transport, str) or transport not in {"http", "stdio"}:
        raise ProxyError(
            f"remote config {path!r}: server.transport must be `http` or `stdio`, "
            f"got {transport!r}"
        )

    public_base_url = server_raw.get("public_base_url")
    if public_base_url is not None and (
        not isinstance(public_base_url, str) or not public_base_url.strip()
    ):
        raise ProxyError(
            f"remote config {path!r}: server.public_base_url must be a non-empty string"
        )
    if isinstance(public_base_url, str):
        public_base_url = public_base_url.strip()
        parsed = urlsplit(public_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProxyError(
                f"remote config {path!r}: server.public_base_url must be an http:// or https:// URL"
            )

    auth_raw = raw.get("auth") or {}
    if not isinstance(auth_raw, dict):
        raise ProxyError(f"remote config {path!r}: auth must be a mapping")
    api_key = auth_raw.get("api_key")
    if api_key is not None and (
        not isinstance(api_key, str) or not api_key.strip()
    ):
        raise ProxyError(
            f"remote config {path!r}: auth.api_key must be a non-empty string"
        )
    if isinstance(api_key, str):
        api_key = api_key.strip()

    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise ProxyError(f"remote config {path!r}: defaults must be a mapping")
    cwd = defaults_raw.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        raise ProxyError(
            f"remote config {path!r}: defaults.cwd must be a non-empty string"
        )
    if isinstance(cwd, str):
        cwd = cwd.strip()

    return RemoteServerMetadata(
        transport=transport,
        public_base_url=public_base_url,
        api_key=api_key,
        cwd=cwd,
    )


async def _discover_remote_stdio_command(host: str) -> list[str]:
    rcm_returncode, rcm_stdout, rcm_stderr = await _ssh_command(
        host,
        "command -v rcm",
    )
    rcm_path = rcm_stdout.strip().splitlines()[0] if rcm_returncode == 0 else ""
    if rcm_path:
        return [rcm_path, "--stdio"]

    uvx_returncode, uvx_stdout, uvx_stderr = await _ssh_command(
        host,
        "command -v uvx",
    )
    uvx_path = uvx_stdout.strip().splitlines()[0] if uvx_returncode == 0 else ""
    if uvx_path:
        return [uvx_path, "rcm", "--stdio"]

    detail = uvx_stderr.strip() or rcm_stderr.strip()
    suffix = f": {detail}" if detail else ""
    raise ProxyError(
        f"neither rcm nor uvx was found on remote host {host!r}{suffix}"
    )


def _remote_http_endpoint(public_base_url: str) -> str:
    endpoint = public_base_url.rstrip("/")
    return endpoint if endpoint.endswith("/mcp") else f"{endpoint}/mcp"


def _remote_sync_spec(
    target: ProxyTargetSpec,
    remote_config: RemoteConfigSpec,
    metadata: RemoteServerMetadata,
) -> SyncSpec | None:
    configured = target.sync
    if configured is not None and not configured.enabled:
        return None

    source = str(Path.cwd())
    destination = metadata.cwd or str(Path(remote_config.path).parent)
    if configured is None:
        return SyncSpec(source=source, destination=destination)
    return replace(
        configured,
        source=configured.source or source,
        destination=configured.destination or destination,
        enabled=True,
    )


async def _resolve_remote_target(target: ProxyTargetSpec) -> ProxyTargetSpec:
    if target.remote_config is None or target.ssh is None:
        raise ProxyError(f"proxy target {target.name!r} is missing remote config or SSH")

    metadata = await _read_remote_metadata(target)
    sync = _remote_sync_spec(target, target.remote_config, metadata)
    if metadata.transport == "http":
        if metadata.public_base_url is None:
            raise ProxyError(
                f"remote config {target.remote_config.path!r}: "
                "server.public_base_url is required for HTTP transport"
            )
        headers = dict(target.headers)
        if metadata.api_key is not None and not any(
            name.lower() == "authorization" for name in headers
        ):
            headers["Authorization"] = HeaderSpec(value=f"Bearer {metadata.api_key}")
        return replace(
            target,
            transport="http",
            endpoint=_remote_http_endpoint(metadata.public_base_url),
            headers=headers,
            sync=sync,
            remote_config=None,
        )

    command = await _discover_remote_stdio_command(target.ssh.host)
    return replace(
        target,
        transport="ssh",
        ssh=replace(target.ssh, command=command),
        sync=sync,
    )


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
        child_env = dict(os.environ)
        child_env[ARTIFACT_ENV] = ARTIFACT_ENV_VALUE
        return StdioTransport(
            command=target.command[0],
            args=target.command[1:],
            cwd=target.cwd,
            env=child_env,
        )

    if target.transport == "ssh":
        assert target.ssh is not None
        if target.ssh.command is None:
            raise ProxyError(f"proxy target {target.name!r} has no SSH command")
        env_args = [f"{ARTIFACT_ENV}={ARTIFACT_ENV_VALUE}"]
        if target.remote_config is not None:
            env_args.append(f"RCM_CONFIG={shlex.quote(target.remote_config.path)}")
        return StdioTransport(
            command="ssh",
            args=[
                target.ssh.host,
                "env",
                *env_args,
                *target.ssh.command,
            ],
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
    _store: Store | None = PrivateAttr()

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
        store: Store | None = None,
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
        self._store = store

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

        structured_content = getattr(result, "structured_content", None)
        if structured_content is None:
            structured_content = getattr(result, "structuredContent", None)
        if isinstance(structured_content, dict) and (
            structured_content.get("artifact_protocol") == ARTIFACT_PROTOCOL
        ):
            try:
                structured_content = self._materialize_artifact(structured_content)
            except (ArtifactError, OSError) as exc:
                raise ToolError(str(exc)) from exc

        return ToolResult(
            content=result.content,
            structured_content=structured_content,
            meta=getattr(result, "meta", None),
            is_error=getattr(result, "is_error", getattr(result, "isError", False)),
        )

    def _materialize_artifact(self, remote: dict[str, Any]) -> dict[str, Any]:
        if self._store is None:
            raise ArtifactError("rcm artifact received without a local store")

        stdout_raw = remote.get("stdout_base64")
        stderr_raw = remote.get("stderr_base64")
        if not isinstance(stdout_raw, str) or not isinstance(stderr_raw, str):
            raise ArtifactError("rcm artifact is missing base64 output fields")
        try:
            stdout = base64.b64decode(stdout_raw, validate=True)
            stderr = base64.b64decode(stderr_raw, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ArtifactError("rcm artifact contains invalid base64 output") from exc

        run_id, _ = self._store.create_run()
        self._store.file_path(run_id, "stdout").write_bytes(stdout)
        self._store.file_path(run_id, "stderr").write_bytes(stderr)

        local = dict(remote)
        local.pop("artifact_protocol", None)
        local.pop("stdout_base64", None)
        local.pop("stderr_base64", None)
        local.pop("stdout_url", None)
        local.pop("stderr_url", None)
        local["run_id"] = run_id
        local["stdout_bytes"] = len(stdout)
        local["stderr_bytes"] = len(stderr)
        local["stdout_url"] = self._store.url_for(run_id, "stdout")
        local["stderr_url"] = self._store.url_for(run_id, "stderr")
        self._store.write_meta(run_id, local)
        return local


class ArtifactError(RuntimeError):
    """Raised when an rcm artifact cannot be recovered locally."""


class ProxyRuntime:
    """Own connected remote clients and the locally registered proxy tools."""

    def __init__(self, server: FastMCP, stack: AsyncExitStack) -> None:
        self.server = server
        self._stack = stack

    @classmethod
    async def create(
        cls,
        cfg: Config,
        api_key: str | None,
        store: Store,
    ) -> ProxyRuntime:
        if cfg.proxy is None:
            raise ProxyError("proxy configuration is missing")

        server = FastMCP("rcm")
        if api_key is not None:
            server.add_middleware(ApiKeyAuth(api_key))
        stack = AsyncExitStack()
        try:
            for configured_target in cfg.proxy.targets:
                target = (
                    await _resolve_remote_target(configured_target)
                    if configured_target.remote_config is not None
                    else configured_target
                )
                client = await stack.enter_async_context(
                    Client(
                        _build_transport(target),
                        name=f"rcm-proxy-{target.name}",
                    )
                )
                remote_tools = await client.list_tools()
                sync_runner = (
                    SyncRunner(target, cfg.config_path)
                    if target.sync is not None and target.sync.enabled
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
                            store=store,
                        )
                    )
        except Exception:
            await stack.aclose()
            raise
        return cls(server, stack)

    async def close(self) -> None:
        await self._stack.aclose()
