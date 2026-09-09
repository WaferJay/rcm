"""Typed configuration models exposed by :mod:`rcm.config`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
PARAM_TYPES = {"string", "integer", "number", "boolean"}
PROXY_TRANSPORTS = {"stdio", "ssh", "http", "sse"}
SERVER_TRANSPORTS = {"http", "stdio"}
COLLECT_ON_EXIT = {"success", "always"}
COLLECT_MODES = {"always", "changed"}


class ConfigError(ValueError):
    """Raised when commands.yaml is invalid."""


@dataclass
class ParamSpec:
    name: str
    type: str
    description: str = ""
    default: Any = None
    has_default: bool = False
    pattern: str | None = None
    enum: list[Any] | None = None


@dataclass(frozen=True)
class CollectPathSpec:
    path: str
    required: bool = False
    on_exit: str | None = None
    mode: str | None = None


@dataclass(frozen=True)
class CollectSpec:
    paths: tuple[CollectPathSpec, ...]
    on_exit: str = "success"
    mode: str = "always"


@dataclass
class CommandSpec:
    name: str
    description: str
    command: list[str]
    params: list[ParamSpec] = field(default_factory=list)
    timeout: float | None = None
    cwd: str | None = None
    collect: CollectSpec | None = None


@dataclass
class TLSConfig:
    enabled: bool = False
    cert_file: str | None = None
    key_file: str | None = None
    auto_generate: bool = False
    hostnames: list[str] = field(default_factory=list)


@dataclass
class ServerSpec:
    host: str | None = None
    port: int | None = None
    public_base_url: str | None = None
    tls: TLSConfig = field(default_factory=TLSConfig)
    transport: str = "http"


@dataclass
class AuthSpec:
    api_key: str | None = None


@dataclass
class DefaultsSpec:
    timeout: float | None = None
    cwd: str | None = None


@dataclass
class HeaderSpec:
    env: str | None = None
    value: str | None = None


@dataclass
class SyncMappingSpec:
    source: str | None = None
    destination: str | None = None
    excludes: list[str] = field(default_factory=list)
    delete: bool = False


@dataclass(init=False)
class SyncSpec:
    mappings: list[SyncMappingSpec] = field(default_factory=list)
    enabled: bool = True

    def __init__(
        self,
        mappings: list[SyncMappingSpec] | None = None,
        enabled: bool = True,
        *,
        source: str | None = None,
        destination: str | None = None,
        excludes: list[str] | None = None,
        delete: bool = False,
    ) -> None:
        """Build a sync spec, accepting the former single-mapping API."""
        legacy = (
            source is not None
            or destination is not None
            or excludes is not None
            or delete
        )
        if mappings is not None and legacy:
            raise TypeError("mappings cannot be combined with legacy sync fields")
        if mappings is None and legacy:
            mappings = [
                SyncMappingSpec(
                    source=source,
                    destination=destination,
                    excludes=list(excludes or []),
                    delete=delete,
                )
            ]
        self.mappings = list(mappings or [])
        self.enabled = enabled

    def _single_mapping(self) -> SyncMappingSpec | None:
        return self.mappings[0] if len(self.mappings) == 1 else None

    @property
    def source(self) -> str | None:
        mapping = self._single_mapping()
        return mapping.source if mapping is not None else None

    @property
    def destination(self) -> str | None:
        mapping = self._single_mapping()
        return mapping.destination if mapping is not None else None

    @property
    def excludes(self) -> list[str]:
        mapping = self._single_mapping()
        return mapping.excludes if mapping is not None else []

    @property
    def delete(self) -> bool:
        mapping = self._single_mapping()
        return mapping.delete if mapping is not None else False


@dataclass
class SSHSpec:
    host: str
    command: list[str] | None = None
    tunnel: bool = False


@dataclass
class RemoteConfigSpec:
    path: str


@dataclass
class ProxyTargetSpec:
    name: str
    transport: str
    command: list[str] | None = None
    cwd: str | None = None
    ssh: SSHSpec | None = None
    endpoint: str | None = None
    headers: dict[str, HeaderSpec] = field(default_factory=dict)
    sync: SyncSpec | None = None
    remote_config: RemoteConfigSpec | None = None
    artifacts: str | None = None


@dataclass
class ProxySpec:
    targets: list[ProxyTargetSpec] = field(default_factory=list)


@dataclass
class Config:
    server: ServerSpec
    auth: AuthSpec
    defaults: DefaultsSpec
    commands: list[CommandSpec]
    proxy: ProxySpec | None = None
    config_path: Path | None = None
