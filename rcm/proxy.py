"""MCP proxy target management and active-sync tool wrappers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

import httpx
import yaml

from fastmcp import Client, FastMCP
from fastmcp.client.transports import (
    SSETransport,
    StdioTransport,
    StreamableHttpTransport,
)
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import Tool, ToolResult
from mcp.types import Implementation, TextContent
from pydantic import PrivateAttr

from . import __version__
from .auth import ApiKeyAuth
from .artifacts import (
    DEFAULT_ARTIFACT_MODE,
    RCM_CALL_META,
    RCM_CAPABILITY,
    RCM_EXPERIMENTAL_CAPABILITIES,
    RCM_PROTOCOL_VERSION,
    RCM_RESULT_SCHEMA,
    ArtifactDescriptor,
    ArtifactError,
    RunResult,
    parse_run_result,
    public_run_result,
)
from .config import (
    Config,
    HeaderSpec,
    ProxyTargetSpec,
    RemoteConfigSpec,
    SyncMappingSpec,
    SyncSpec,
)
from .store import Store, StoreError
from .sync import SyncError, SyncRunner


logger = logging.getLogger(__name__)


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
            stdin=asyncio.subprocess.DEVNULL,
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
    destination_base = metadata.cwd or str(PurePosixPath(remote_config.path).parent)
    if configured is None:
        logger.warning(
            "proxy target %r is using implicit full-directory sync (%s -> %s); "
            "configure sync.mappings or sync.enabled: false to make this explicit",
            target.name,
            source,
            destination_base,
        )
        return SyncSpec(
            mappings=[
                SyncMappingSpec(source=source, destination=destination_base)
            ]
        )

    mappings: list[SyncMappingSpec] = []
    for mapping in configured.mappings:
        destination = mapping.destination
        if destination is None:
            destination = destination_base
        else:
            first_slash = destination.find("/")
            colon = destination.find(":")
            host_qualified = colon >= 0 and (first_slash < 0 or colon < first_slash)
            if not destination.startswith("/") and not host_qualified:
                destination = posixpath.join(destination_base, destination)
        mappings.append(
            replace(
                mapping,
                source=mapping.source or source,
                destination=destination,
            )
        )
    return replace(
        configured,
        mappings=mappings,
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


def _build_transport(target: ProxyTargetSpec):
    if target.transport == "stdio":
        assert target.command is not None
        return StdioTransport(
            command=target.command[0],
            args=target.command[1:],
            cwd=target.cwd,
            env=dict(os.environ),
        )

    if target.transport == "ssh":
        assert target.ssh is not None
        if target.ssh.command is None:
            raise ProxyError(f"proxy target {target.name!r} has no SSH command")
        remote_args: list[str] = []
        if target.remote_config is not None:
            remote_args.extend(
                ["env", f"RCM_CONFIG={shlex.quote(target.remote_config.path)}"]
            )
        return StdioTransport(
            command="ssh",
            args=[
                target.ssh.host,
                *remote_args,
                *target.ssh.command,
            ],
        )

    assert target.endpoint is not None
    headers = _resolve_headers(target)
    if target.transport == "http":
        return StreamableHttpTransport(target.endpoint, headers=headers)
    return SSETransport(target.endpoint, headers=headers)


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


def _file_path_from_uri(uri: str) -> str:
    try:
        parsed = urlsplit(uri)
    except ValueError as exc:
        raise ArtifactError("artifact has an invalid file URI") from exc
    if parsed.scheme.lower() != "file":
        raise ArtifactError("artifact URI is not a file URI")
    if parsed.netloc or parsed.query or parsed.fragment:
        raise ArtifactError("file artifact URI cannot contain a host, query, or fragment")
    if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path):
        raise ArtifactError("file artifact URI contains invalid percent encoding")
    raw_path = unquote_to_bytes(parsed.path)
    if b"\x00" in raw_path:
        raise ArtifactError("file artifact URI contains a NUL byte")
    path = os.fsdecode(raw_path)
    if not Path(path).is_absolute():
        raise ArtifactError("file artifact URI must contain an absolute path")
    return path


def _validate_transfer(
    descriptor: ArtifactDescriptor, size: int, digest: str
) -> None:
    if size != descriptor.bytes:
        raise ArtifactError(
            f"artifact size mismatch: expected {descriptor.bytes}, received {size}"
        )
    if digest != descriptor.sha256:
        raise ArtifactError("artifact SHA-256 mismatch")


def _copy_local_file(
    source: str, destination: Path, descriptor: ArtifactDescriptor
) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        source_stream = open(source, "rb")
    except OSError as exc:
        raise ArtifactError(f"failed to open local artifact {source!r}: {exc}") from exc
    with source_stream, destination.open("wb") as output:
        while chunk := source_stream.read(1024 * 1024):
            size += len(chunk)
            if size > descriptor.bytes:
                raise ArtifactError(
                    f"artifact exceeds declared size of {descriptor.bytes} bytes"
                )
            digest.update(chunk)
            output.write(chunk)
    _validate_transfer(descriptor, size, digest.hexdigest())


async def _read_ssh_diagnostics(stream: asyncio.StreamReader) -> bytes:
    captured = bytearray()
    while chunk := await stream.read(64 * 1024):
        remaining = (64 * 1024) - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return bytes(captured)


async def _copy_ssh_file(
    host: str,
    source: str,
    destination: Path,
    descriptor: ArtifactDescriptor,
) -> None:
    command = f"cat -- {shlex.quote(source)}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            host,
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ArtifactError(
            f"failed to execute ssh for artifact on {host!r}: {exc}"
        ) from exc

    assert proc.stdout is not None
    assert proc.stderr is not None
    diagnostics_task = asyncio.create_task(_read_ssh_diagnostics(proc.stderr))
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("wb") as output:
            while chunk := await proc.stdout.read(1024 * 1024):
                size += len(chunk)
                if size > descriptor.bytes:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    raise ArtifactError(
                        f"artifact exceeds declared size of {descriptor.bytes} bytes"
                    )
                digest.update(chunk)
                output.write(chunk)
        returncode = await proc.wait()
        diagnostics = await diagnostics_task
    except BaseException:
        if not diagnostics_task.done():
            diagnostics_task.cancel()
        try:
            await diagnostics_task
        except BaseException:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except BaseException:
            pass
        raise

    if returncode != 0:
        detail = diagnostics.decode(errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ArtifactError(
            f"failed to read remote artifact {source!r} from {host!r} "
            f"(exit code {returncode}){suffix}"
        )
    _validate_transfer(descriptor, size, digest.hexdigest())


async def _copy_http_file(
    destination: Path,
    descriptor: ArtifactDescriptor,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    try:
        parsed = urlsplit(descriptor.uri)
    except ValueError as exc:
        raise ArtifactError("HTTP artifact URI is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ArtifactError("HTTP artifact URI must be an absolute http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise ArtifactError("HTTP artifact URI cannot contain user information")

    digest = hashlib.sha256()
    size = 0
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=5,
            timeout=httpx.Timeout(30.0),
            transport=transport,
        ) as client:
            async with client.stream(
                "GET", descriptor.uri, headers={"Accept-Encoding": "identity"}
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise ArtifactError(
                        f"artifact download returned HTTP {response.status_code}"
                    )
                if response.url.scheme.lower() not in {"http", "https"}:
                    raise ArtifactError("artifact redirect used a non-HTTP URL")
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > descriptor.bytes:
                            raise ArtifactError(
                                "artifact exceeds declared size of "
                                f"{descriptor.bytes} bytes"
                            )
                        digest.update(chunk)
                        output.write(chunk)
    except ArtifactError:
        raise
    except httpx.HTTPError as exc:
        raise ArtifactError(f"artifact download failed: {exc}") from exc
    _validate_transfer(descriptor, size, digest.hexdigest())


class ProxyTool(Tool):
    """A FastMCP tool that syncs and forwards one remote MCP tool call."""

    _target_name: str = PrivateAttr()
    _remote_name: str = PrivateAttr()
    _client: Client = PrivateAttr()
    _sync_runner: SyncRunner | None = PrivateAttr()
    _store: Store | None = PrivateAttr()
    _target_transport: str = PrivateAttr()
    _ssh_host: str | None = PrivateAttr()
    _artifact_mode: str = PrivateAttr()
    _rcm_peer: bool = PrivateAttr()

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
        target_transport: str = "stdio",
        ssh_host: str | None = None,
        artifact_mode: str = DEFAULT_ARTIFACT_MODE,
        rcm_peer: bool = False,
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
        self._target_transport = target_transport
        self._ssh_host = ssh_host
        self._artifact_mode = artifact_mode
        self._rcm_peer = rcm_peer

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        if self._sync_runner is not None:
            try:
                await self._sync_runner.sync()
            except SyncError as exc:
                raise ToolError(str(exc)) from exc

        try:
            call_kwargs: dict[str, Any] = {}
            if self._rcm_peer:
                call_kwargs["meta"] = RCM_CALL_META
            result = await self._client.call_tool_mcp(
                self._remote_name, arguments or {}, **call_kwargs
            )
        except Exception as exc:
            raise ToolError(
                f"proxy target {self._target_name!r} tool "
                f"{self._remote_name!r} failed: {exc}"
            ) from exc

        structured_content = getattr(result, "structured_content", None)
        if structured_content is None:
            structured_content = getattr(result, "structuredContent", None)
        rewritten = False
        if (
            self._rcm_peer
            and isinstance(structured_content, dict)
            and structured_content.get("schema") == RCM_RESULT_SCHEMA
        ):
            try:
                remote = parse_run_result(structured_content)
                if self._artifact_mode == "passthrough":
                    self._validate_passthrough(remote)
                else:
                    structured_content = await self._materialize_artifact(remote)
                rewritten = True
            except (ArtifactError, OSError, StoreError) as exc:
                raise ToolError(str(exc)) from exc

        return ToolResult(
            content=(
                [
                    TextContent(
                        type="text",
                        text=json.dumps(structured_content, ensure_ascii=False),
                    )
                ]
                if rewritten
                else result.content
            ),
            structured_content=structured_content,
            meta=getattr(result, "meta", None),
            is_error=getattr(result, "is_error", getattr(result, "isError", False)),
        )

    def _validate_passthrough(self, remote: RunResult) -> None:
        if self._target_transport != "http":
            raise ArtifactError("artifact passthrough requires an HTTP RCM target")
        for descriptor in (remote.stdout, remote.stderr):
            try:
                parsed = urlsplit(descriptor.uri)
            except ValueError as exc:
                raise ArtifactError("artifact passthrough URL is invalid") from exc
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                raise ArtifactError(
                    "artifact passthrough requires absolute HTTP stdout/stderr URLs"
                )
            if parsed.username is not None or parsed.password is not None:
                raise ArtifactError(
                    "artifact passthrough URLs cannot contain user information"
                )

    async def _copy_artifact(
        self, descriptor: ArtifactDescriptor, destination: Path
    ) -> None:
        try:
            scheme = urlsplit(descriptor.uri).scheme.lower()
        except ValueError as exc:
            raise ArtifactError("artifact URI is invalid") from exc
        if scheme in {"http", "https"}:
            await _copy_http_file(destination, descriptor)
            return
        if scheme != "file":
            raise ArtifactError(f"unsupported artifact URI scheme {scheme!r}")

        source = _file_path_from_uri(descriptor.uri)
        if self._target_transport == "stdio":
            await asyncio.to_thread(_copy_local_file, source, destination, descriptor)
            return
        if self._target_transport == "ssh":
            if self._ssh_host is None:
                raise ArtifactError("SSH artifact received without an SSH host")
            await _copy_ssh_file(self._ssh_host, source, destination, descriptor)
            return
        raise ArtifactError(
            f"{self._target_transport} RCM target returned an inaccessible file URI"
        )

    async def _materialize_artifact(self, remote: RunResult) -> dict[str, Any]:
        if self._store is None:
            raise ArtifactError("rcm artifact received without a local store")
        run_id, staging = self._store.create_staging_run()
        try:
            await self._copy_artifact(remote.stdout, staging / "stdout.log")
            await self._copy_artifact(remote.stderr, staging / "stderr.log")
            local = public_run_result(
                run_id=run_id,
                returncode=remote.returncode,
                timed_out=remote.timed_out,
                duration_ms=remote.duration_ms,
                stdout_uri=self._store.url_for(run_id, "stdout"),
                stdout_bytes=remote.stdout.bytes,
                stdout_sha256=remote.stdout.sha256,
                stderr_uri=self._store.url_for(run_id, "stderr"),
                stderr_bytes=remote.stderr.bytes,
                stderr_sha256=remote.stderr.sha256,
            )
            meta = {
                **local,
                "upstream": {
                    "target": self._target_name,
                    "run_id": remote.run_id,
                    "transport": self._target_transport,
                },
            }
            self._store.commit_staging_run(run_id, staging, meta)
            return local
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise


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

        server = FastMCP(
            "rcm",
            version=__version__,
            experimental_capabilities=RCM_EXPERIMENTAL_CAPABILITIES,
        )
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
                client = await stack.enter_async_context(
                    Client(
                        _build_transport(target),
                        name=f"rcm-proxy-{target.name}",
                        client_info=Implementation(name="rcm", version=__version__),
                    )
                )
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
                remote_tools = await client.list_tools()
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
                            ssh_host=(target.ssh.host if target.ssh is not None else None),
                            artifact_mode=artifact_mode,
                            rcm_peer=rcm_peer,
                        )
                    )
        except Exception:
            await stack.aclose()
            raise
        return cls(server, stack)

    async def close(self) -> None:
        await self._stack.aclose()
