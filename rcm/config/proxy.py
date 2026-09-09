"""Parsing for proxy targets, transports, and synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..artifacts import ARTIFACT_MODES
from .models import (
    NAME_RE,
    PROXY_TRANSPORTS,
    ConfigError,
    HeaderSpec,
    ProxySpec,
    ProxyTargetSpec,
    RemoteConfigSpec,
    SSHSpec,
    SyncMappingSpec,
    SyncSpec,
)
from .validation import (
    parse_non_empty_argv,
    parse_optional_non_empty_string,
    parse_sync_destination,
    validate_proxy_glob,
)


def _parse_sync_mapping(
    raw: Any,
    context: str,
    *,
    require_paths: bool,
) -> SyncMappingSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"{context} must be a mapping")

    source = raw.get("source")
    if source is not None and (not isinstance(source, str) or not source.strip()):
        raise ConfigError(f"{context}.source must be a non-empty string")
    destination = parse_sync_destination(raw.get("destination"), context)
    if require_paths and source is None:
        raise ConfigError(f"{context}.source is required")
    if require_paths and destination is None:
        raise ConfigError(f"{context}.destination is required")

    excludes_raw = raw.get("excludes", [])
    if not isinstance(excludes_raw, list):
        raise ConfigError(f"{context}.excludes must be a list")
    excludes = [
        validate_proxy_glob(pattern, index, context)
        for index, pattern in enumerate(excludes_raw)
    ]

    delete = raw.get("delete", False)
    if not isinstance(delete, bool):
        raise ConfigError(f"{context}.delete must be a boolean")
    return SyncMappingSpec(
        source=source.strip() if isinstance(source, str) else None,
        destination=destination,
        excludes=excludes,
        delete=delete,
    )


def _parse_headers(raw: Any, target_name: str) -> dict[str, HeaderSpec]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"proxy.{target_name}.headers must be a mapping")

    headers: dict[str, HeaderSpec] = {}
    for header_name, header_raw in raw.items():
        if not isinstance(header_name, str) or not header_name.strip():
            raise ConfigError(
                f"proxy.{target_name}.headers names must be non-empty strings"
            )
        if not isinstance(header_raw, dict):
            raise ConfigError(
                f"proxy.{target_name}.headers.{header_name} must be a mapping"
            )
        keys = set(header_raw)
        if keys != {"env"} and keys != {"value"}:
            raise ConfigError(
                f"proxy.{target_name}.headers.{header_name} must contain "
                "exactly one of `env` or `value`"
            )
        source = next(iter(keys))
        value = header_raw[source]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"proxy.{target_name}.headers.{header_name}.{source} "
                "must be a non-empty string"
            )
        headers[header_name.strip()] = HeaderSpec(**{source: value})
    return headers


def _parse_sync(raw: Any, target_name: str) -> SyncSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"proxy.{target_name}.sync must be a mapping")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"proxy.{target_name}.sync.enabled must be a boolean")

    legacy_keys = {"source", "destination", "excludes", "delete"}
    has_legacy = any(key in raw for key in legacy_keys)
    has_mappings = "mappings" in raw
    if has_legacy and has_mappings:
        raise ConfigError(
            f"proxy.{target_name}.sync cannot combine `mappings` with legacy "
            "source/destination/excludes/delete fields"
        )
    if not enabled:
        if has_legacy or has_mappings:
            raise ConfigError(
                f"proxy.{target_name}.sync cannot configure mappings "
                "when enabled is false"
            )
        return SyncSpec(enabled=False)

    context = f"proxy.{target_name}.sync"
    if has_mappings:
        mappings_raw = raw["mappings"]
        if not isinstance(mappings_raw, list) or not mappings_raw:
            raise ConfigError(f"{context}.mappings must be a non-empty list")
        mappings = [
            _parse_sync_mapping(
                mapping_raw,
                f"{context}.mappings[{index}]",
                require_paths=True,
            )
            for index, mapping_raw in enumerate(mappings_raw)
        ]
    else:
        mappings = [_parse_sync_mapping(raw, context, require_paths=False)]
    return SyncSpec(mappings=mappings, enabled=True)


def _parse_remote_config(raw: Any, target_name: str) -> RemoteConfigSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip() or not raw.strip().startswith("/"):
        raise ConfigError(
            f"proxy.{target_name}.config must be an absolute, non-empty path"
        )
    return RemoteConfigSpec(path=raw.strip())


def _parse_artifacts(raw: Any, target_name: str, transport: str) -> str | None:
    if raw is not None and (
        not isinstance(raw, str) or raw not in ARTIFACT_MODES
    ):
        raise ConfigError(
            f"proxy.{target_name}.artifacts must be one of {sorted(ARTIFACT_MODES)}, "
            f"got {raw!r}"
        )
    if raw == "passthrough" and transport not in {"http", "remote"}:
        raise ConfigError(
            f"proxy.{target_name}.artifacts passthrough requires an HTTP RCM target"
        )
    return raw


def _parse_ssh(raw: Any, target_name: str) -> SSHSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"proxy.{target_name}.ssh must be a mapping")
    host = parse_optional_non_empty_string(
        raw.get("host"),
        f"proxy.{target_name}.ssh.host",
    )
    if host is None:
        raise ConfigError(f"proxy.{target_name}.ssh.host must be a non-empty string")
    command = parse_non_empty_argv(
        raw.get("command"),
        f"proxy.{target_name}.ssh.command",
    )
    tunnel = raw.get("tunnel", False)
    if not isinstance(tunnel, bool):
        raise ConfigError(f"proxy.{target_name}.ssh.tunnel must be a boolean")
    return SSHSpec(host=host, command=command, tunnel=tunnel)


def _parse_endpoint(raw: Any, target_name: str) -> str | None:
    if raw is None:
        return None
    if (
        not isinstance(raw, str)
        or not raw.strip()
        or not raw.strip().startswith(("http://", "https://"))
    ):
        raise ConfigError(
            f"proxy.{target_name}.endpoint must be an http:// or https:// URL"
        )
    return raw.strip()


def _validate_explicit_sync(target_name: str, sync: SyncSpec | None) -> None:
    if sync is not None and sync.enabled and any(
        mapping.source is None or mapping.destination is None
        for mapping in sync.mappings
    ):
        raise ConfigError(
            f"proxy.{target_name}.sync.source and sync.destination are required "
            "for explicit transports"
        )


@dataclass(frozen=True)
class _StdioTarget:
    name: str
    command: list[str]
    cwd: str | None
    sync: SyncSpec | None
    artifacts: str | None

    def to_spec(self) -> ProxyTargetSpec:
        return ProxyTargetSpec(
            name=self.name,
            transport="stdio",
            command=self.command,
            cwd=self.cwd,
            sync=self.sync,
            artifacts=self.artifacts,
        )


@dataclass(frozen=True)
class _SSHTarget:
    name: str
    ssh: SSHSpec
    sync: SyncSpec | None
    artifacts: str | None

    def to_spec(self) -> ProxyTargetSpec:
        return ProxyTargetSpec(
            name=self.name,
            transport="ssh",
            ssh=self.ssh,
            sync=self.sync,
            artifacts=self.artifacts,
        )


@dataclass(frozen=True)
class _HttpTarget:
    name: str
    transport: str
    endpoint: str
    headers: dict[str, HeaderSpec]
    sync: SyncSpec | None
    artifacts: str | None

    def to_spec(self) -> ProxyTargetSpec:
        return ProxyTargetSpec(
            name=self.name,
            transport=self.transport,
            endpoint=self.endpoint,
            headers=self.headers,
            sync=self.sync,
            artifacts=self.artifacts,
        )


@dataclass(frozen=True)
class _RemoteTarget:
    name: str
    ssh: SSHSpec
    remote_config: RemoteConfigSpec
    headers: dict[str, HeaderSpec]
    sync: SyncSpec | None
    artifacts: str | None

    def to_spec(self) -> ProxyTargetSpec:
        return ProxyTargetSpec(
            name=self.name,
            transport="remote",
            ssh=self.ssh,
            headers=self.headers,
            sync=self.sync,
            remote_config=self.remote_config,
            artifacts=self.artifacts,
        )


def _build_stdio_target(
    name: str,
    raw: dict[str, Any],
    *,
    command: list[str] | None,
    cwd: str | None,
    ssh: SSHSpec | None,
    endpoint: str | None,
    artifacts: str | None,
) -> _StdioTarget:
    if command is None:
        raise ConfigError(f"proxy.{name}.command is required for stdio transport")
    if ssh is not None and ssh.tunnel:
        raise ConfigError(
            f"proxy.{name}.ssh.tunnel is only valid when config is specified"
        )
    if ssh is not None:
        raise ConfigError(f"proxy.{name}.ssh is only valid for ssh transport")
    if endpoint is not None:
        raise ConfigError(
            f"proxy.{name}.endpoint is only valid for http or sse transport"
        )
    if raw.get("headers") is not None:
        raise ConfigError(
            f"proxy.{name}.headers is only valid for http or sse transport"
        )
    sync = _parse_sync(raw.get("sync"), name)
    _validate_explicit_sync(name, sync)
    return _StdioTarget(name, command, cwd, sync, artifacts)


def _build_ssh_target(
    name: str,
    raw: dict[str, Any],
    *,
    command: list[str] | None,
    cwd: str | None,
    ssh: SSHSpec | None,
    endpoint: str | None,
    artifacts: str | None,
) -> _SSHTarget:
    if ssh is None:
        raise ConfigError(f"proxy.{name}.ssh is required for ssh transport")
    if ssh.command is None:
        raise ConfigError(f"proxy.{name}.ssh.command is required for ssh transport")
    if command is not None:
        raise ConfigError(f"proxy.{name}.command is not used with ssh transport")
    if ssh.tunnel:
        raise ConfigError(
            f"proxy.{name}.ssh.tunnel is only valid when config is specified"
        )
    if endpoint is not None:
        raise ConfigError(
            f"proxy.{name}.endpoint is only valid for http or sse transport"
        )
    if raw.get("headers") is not None:
        raise ConfigError(
            f"proxy.{name}.headers is only valid for http or sse transport"
        )
    if cwd is not None:
        raise ConfigError(f"proxy.{name}.cwd is only valid for stdio transport")
    sync = _parse_sync(raw.get("sync"), name)
    _validate_explicit_sync(name, sync)
    return _SSHTarget(name, ssh, sync, artifacts)


def _build_http_target(
    name: str,
    transport: str,
    raw: dict[str, Any],
    *,
    command: list[str] | None,
    cwd: str | None,
    ssh: SSHSpec | None,
    endpoint: str | None,
    artifacts: str | None,
) -> _HttpTarget:
    if endpoint is None:
        raise ConfigError(
            f"proxy.{name}.endpoint is required for {transport} transport"
        )
    if command is not None:
        raise ConfigError(
            f"proxy.{name}.command is not used with {transport} transport"
        )
    if ssh is not None and ssh.tunnel:
        raise ConfigError(
            f"proxy.{name}.ssh.tunnel is only valid when config is specified"
        )
    if ssh is not None:
        raise ConfigError(f"proxy.{name}.ssh is only valid for ssh transport")
    if cwd is not None:
        raise ConfigError(f"proxy.{name}.cwd is only valid for stdio transport")
    sync = _parse_sync(raw.get("sync"), name)
    _validate_explicit_sync(name, sync)
    return _HttpTarget(
        name,
        transport,
        endpoint,
        _parse_headers(raw.get("headers"), name),
        sync,
        artifacts,
    )


def _build_remote_target(
    name: str,
    raw: dict[str, Any],
    *,
    remote_config: RemoteConfigSpec,
    command: list[str] | None,
    cwd: str | None,
    ssh: SSHSpec | None,
    endpoint: str | None,
    artifacts: str | None,
) -> _RemoteTarget:
    if ssh is None:
        raise ConfigError(f"proxy.{name}.ssh is required when config is specified")
    if ssh.command is not None:
        raise ConfigError(
            f"proxy.{name}.ssh.command must not be specified when config is specified"
        )
    if ssh.tunnel and artifacts == "passthrough":
        raise ConfigError(
            f"proxy.{name}.artifacts passthrough is not supported with an SSH tunnel"
        )
    if command is not None:
        raise ConfigError(f"proxy.{name} cannot combine config with command")
    if endpoint is not None:
        raise ConfigError(f"proxy.{name} cannot combine config with endpoint")
    if cwd is not None:
        raise ConfigError(f"proxy.{name} cannot combine config with cwd")
    sync = _parse_sync(raw.get("sync"), name)
    return _RemoteTarget(
        name,
        ssh,
        remote_config,
        _parse_headers(raw.get("headers"), name),
        sync,
        artifacts,
    )


def _parse_proxy_target(name: str, raw: Any) -> ProxyTargetSpec:
    if not NAME_RE.fullmatch(name):
        raise ConfigError(
            f"proxy target name must match {NAME_RE.pattern!r}, got {name!r}"
        )
    if not isinstance(raw, dict):
        raise ConfigError(f"proxy.{name} must be a mapping")

    remote_config = _parse_remote_config(raw.get("config"), name)
    transport = raw.get("transport")
    if remote_config is not None:
        if transport is not None:
            raise ConfigError(f"proxy.{name} cannot combine config with transport")
        transport = "remote"
    elif not isinstance(transport, str) or transport not in PROXY_TRANSPORTS:
        raise ConfigError(
            f"proxy.{name}.transport must be one of {sorted(PROXY_TRANSPORTS)}, "
            f"got {transport!r}"
        )

    artifacts = _parse_artifacts(raw.get("artifacts"), name, transport)
    command = parse_non_empty_argv(raw.get("command"), f"proxy.{name}.command")
    cwd = parse_optional_non_empty_string(raw.get("cwd"), f"proxy.{name}.cwd")
    ssh = _parse_ssh(raw.get("ssh"), name)
    endpoint = _parse_endpoint(raw.get("endpoint"), name)

    if transport == "stdio":
        target = _build_stdio_target(
            name,
            raw,
            command=command,
            cwd=cwd,
            ssh=ssh,
            endpoint=endpoint,
            artifacts=artifacts,
        )
    elif transport == "ssh":
        target = _build_ssh_target(
            name,
            raw,
            command=command,
            cwd=cwd,
            ssh=ssh,
            endpoint=endpoint,
            artifacts=artifacts,
        )
    elif transport in {"http", "sse"}:
        target = _build_http_target(
            name,
            transport,
            raw,
            command=command,
            cwd=cwd,
            ssh=ssh,
            endpoint=endpoint,
            artifacts=artifacts,
        )
    else:
        assert remote_config is not None
        target = _build_remote_target(
            name,
            raw,
            remote_config=remote_config,
            command=command,
            cwd=cwd,
            ssh=ssh,
            endpoint=endpoint,
            artifacts=artifacts,
        )
    return target.to_spec()


def _parse_proxy(raw: Any) -> ProxySpec:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("`proxy` must be a non-empty mapping")
    names: set[str] = set()
    targets: list[ProxyTargetSpec] = []
    for name, target_raw in raw.items():
        if name in names:
            raise ConfigError(f"duplicate proxy target name: {name!r}")
        names.add(name)
        if name == "sync" and isinstance(target_raw, dict) and any(
            key in target_raw
            for key in {
                "enabled",
                "source",
                "destination",
                "excludes",
                "delete",
                "mappings",
            }
        ):
            raise ConfigError(
                "`proxy.sync` is not a top-level setting; put it under a proxy "
                "target, for example `proxy.<target>.sync`"
            )
        targets.append(_parse_proxy_target(name, target_raw))
    return ProxySpec(targets=targets)
