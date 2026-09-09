"""Remote RCM discovery and proxy-target resolution."""

from __future__ import annotations

import asyncio
import logging
import posixpath
import shlex
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from ..config import (
    ConfigError,
    HeaderSpec,
    ProxyTargetSpec,
    RemoteConfigSpec,
    SyncMappingSpec,
    SyncSpec,
)
from ..config.loader import decode_yaml_mapping
from ..config.parsing import parse_runtime_sections
from ..tunnel import (
    ArtifactRoute,
    TunnelError,
    derive_remote_http_binding,
)
from .errors import ProxyError


logger = logging.getLogger("rcm.proxy")


@dataclass(frozen=True)
class RemoteServerMetadata:
    transport: str
    public_base_url: str | None = None
    api_key: str | None = None
    cwd: str | None = None
    host: str = "0.0.0.0"
    port: int = 8000
    tls_enabled: bool = False


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

    source = f"remote config {path!r}"
    try:
        raw = decode_yaml_mapping(
            stdout,
            source,
            mapping_error=f"{source} must contain a mapping",
        )
        runtime = parse_runtime_sections(
            raw,
            error_prefix=f"{source}: ",
            quote_names=False,
            transport_choices="`http` or `stdio`",
        )
    except ConfigError as exc:
        raise ProxyError(str(exc)) from exc

    server_raw = runtime.server
    transport = runtime.transport

    public_base_url = server_raw.get("public_base_url")
    if public_base_url is not None and (
        not isinstance(public_base_url, str) or not public_base_url.strip()
    ):
        raise ProxyError(
            f"remote config {path!r}: server.public_base_url must be a non-empty string"
        )
    if isinstance(public_base_url, str):
        public_base_url = public_base_url.strip()
        try:
            parsed = urlsplit(public_base_url)
            _ = parsed.port
        except ValueError as exc:
            raise ProxyError(
                f"remote config {path!r}: server.public_base_url is invalid"
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProxyError(
                f"remote config {path!r}: server.public_base_url must be an http:// or https:// URL"
            )

    auth_raw = runtime.auth
    api_key = auth_raw.get("api_key")
    if api_key is not None and (
        not isinstance(api_key, str) or not api_key.strip()
    ):
        raise ProxyError(
            f"remote config {path!r}: auth.api_key must be a non-empty string"
        )
    if isinstance(api_key, str):
        api_key = api_key.strip()

    defaults_raw = runtime.defaults
    cwd = defaults_raw.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        raise ProxyError(
            f"remote config {path!r}: defaults.cwd must be a non-empty string"
        )
    if isinstance(cwd, str):
        cwd = cwd.strip()

    host = "0.0.0.0"
    port = 8000
    tls_enabled = False
    if target.ssh.tunnel:
        host_raw = server_raw.get("host")
        if host_raw is not None:
            if not isinstance(host_raw, str) or not host_raw.strip():
                raise ProxyError(
                    f"remote config {path!r}: server.host must be a non-empty string"
                )
            host = host_raw.strip()

        port_raw = server_raw.get("port")
        if port_raw is not None:
            if (
                isinstance(port_raw, bool)
                or not isinstance(port_raw, int)
                or not 1 <= port_raw <= 65535
            ):
                raise ProxyError(
                    f"remote config {path!r}: server.port must be an integer "
                    "between 1 and 65535"
                )
            port = port_raw

        tls_raw = server_raw.get("tls")
        if tls_raw is None:
            tls_raw = {}
        if not isinstance(tls_raw, dict):
            raise ProxyError(f"remote config {path!r}: server.tls must be a mapping")
        tls_enabled_raw = tls_raw.get("enabled", False)
        if not isinstance(tls_enabled_raw, bool):
            raise ProxyError(
                f"remote config {path!r}: server.tls.enabled must be a boolean"
            )
        tls_enabled = tls_enabled_raw

    return RemoteServerMetadata(
        transport=transport,
        public_base_url=public_base_url,
        api_key=api_key,
        cwd=cwd,
        host=host,
        port=port,
        tls_enabled=tls_enabled,
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


async def _resolve_remote_target_context(
    target: ProxyTargetSpec,
) -> tuple[ProxyTargetSpec, RemoteServerMetadata]:
    if target.remote_config is None or target.ssh is None:
        raise ProxyError(
            f"proxy target {target.name!r} is missing remote config or SSH"
        )

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
        endpoint = _remote_http_endpoint(metadata.public_base_url)
        artifacts = target.artifacts
        if target.ssh.tunnel:
            if target.artifacts == "passthrough":
                raise ProxyError(
                    f"proxy target {target.name!r}: artifact passthrough is not "
                    "supported with an SSH tunnel"
                )
            try:
                binding = derive_remote_http_binding(
                    metadata.host,
                    metadata.port,
                    tls_enabled=metadata.tls_enabled,
                )
                endpoint = ArtifactRoute.create(
                    metadata.public_base_url,
                    binding,
                ).mcp_endpoint
            except TunnelError as exc:
                raise ProxyError(
                    f"proxy target {target.name!r}: invalid SSH tunnel "
                    f"configuration: {exc}"
                ) from exc
            artifacts = artifacts or "localize"
        return replace(
            target,
            transport="http",
            endpoint=endpoint,
            headers=headers,
            sync=sync,
            remote_config=None,
            artifacts=artifacts,
        ), metadata

    if target.ssh.tunnel:
        raise ProxyError(
            f"proxy target {target.name!r}: ssh.tunnel requires the remote "
            "server transport to be HTTP"
        )

    command = await _discover_remote_stdio_command(target.ssh.host)
    return replace(
        target,
        transport="ssh",
        ssh=replace(target.ssh, command=command),
        sync=sync,
    ), metadata


async def _resolve_remote_target(target: ProxyTargetSpec) -> ProxyTargetSpec:
    resolved, _ = await _resolve_remote_target_context(target)
    return resolved
