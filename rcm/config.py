"""YAML configuration loader and validator for rcm."""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
PARAM_TYPES = {"string", "integer", "number", "boolean"}
PROXY_TRANSPORTS = {"stdio", "ssh", "http", "sse"}
SERVER_TRANSPORTS = {"http", "stdio"}


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


@dataclass
class CommandSpec:
    name: str
    description: str
    command: list[str]
    params: list[ParamSpec] = field(default_factory=list)
    timeout: float | None = None
    cwd: str | None = None


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


def _placeholders(s: str) -> list[str]:
    """Return the set of `{name}` placeholders in a string (Python format syntax)."""
    out: list[str] = []
    for _, field_name, _, _ in string.Formatter().parse(s):
        if field_name is None:
            continue
        if not field_name:
            raise ConfigError(f"empty placeholder in {s!r}")
        if "." in field_name or "[" in field_name:
            raise ConfigError(f"unsupported placeholder syntax in {s!r}")
        out.append(field_name)
    return out


def _parse_param(name: str, raw: dict[str, Any]) -> ParamSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"params.{name} must be a mapping")
    ptype = raw.get("type", "string")
    if ptype not in PARAM_TYPES:
        raise ConfigError(
            f"params.{name}.type must be one of {sorted(PARAM_TYPES)}, got {ptype!r}"
        )
    spec = ParamSpec(
        name=name,
        type=ptype,
        description=str(raw.get("description", "")),
    )
    if "default" in raw:
        spec.default = raw["default"]
        spec.has_default = True
    if "pattern" in raw:
        pattern = raw["pattern"]
        if not isinstance(pattern, str):
            raise ConfigError(f"params.{name}.pattern must be a string")
        try:
            re.compile(pattern)
        except re.error as e:
            raise ConfigError(f"params.{name}.pattern is not a valid regex: {e}")
        spec.pattern = pattern
    if "enum" in raw:
        enum = raw["enum"]
        if not isinstance(enum, list) or not enum:
            raise ConfigError(f"params.{name}.enum must be a non-empty list")
        spec.enum = enum
    return spec


def _parse_command(raw: dict[str, Any]) -> CommandSpec:
    if not isinstance(raw, dict):
        raise ConfigError("each entry in `commands` must be a mapping")

    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ConfigError(
            f"command name must match {NAME_RE.pattern!r}, got {name!r}"
        )

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ConfigError(f"command {name!r}: description is required")

    command = raw.get("command")
    if not isinstance(command, list) or not command:
        raise ConfigError(
            f"command {name!r}: `command` must be a non-empty list of strings (argv form)"
        )
    for i, part in enumerate(command):
        if not isinstance(part, str):
            raise ConfigError(
                f"command {name!r}: command[{i}] must be a string, got {type(part).__name__}"
            )

    params_raw = raw.get("params") or {}
    if not isinstance(params_raw, dict):
        raise ConfigError(f"command {name!r}: `params` must be a mapping")
    params = [_parse_param(pname, pspec) for pname, pspec in params_raw.items()]
    declared = {p.name for p in params}

    used: set[str] = set()
    for part in command:
        for ph in _placeholders(part):
            if ph not in declared:
                raise ConfigError(
                    f"command {name!r}: placeholder {{{ph}}} has no matching params entry"
                )
            used.add(ph)
    unused = declared - used
    if unused:
        raise ConfigError(
            f"command {name!r}: declared params not used in command: {sorted(unused)}"
        )

    timeout = raw.get("timeout")
    if timeout is not None and not isinstance(timeout, (int, float)):
        raise ConfigError(f"command {name!r}: timeout must be a number")
    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ConfigError(f"command {name!r}: cwd must be a string")

    return CommandSpec(
        name=name,
        description=description.strip(),
        command=command,
        params=params,
        timeout=float(timeout) if timeout is not None else None,
        cwd=cwd,
    )


def _validate_proxy_glob(pattern: Any, index: int, context: str) -> str:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ConfigError(f"{context}.excludes[{index}] must be a non-empty string")
    pattern = pattern.strip()
    if pattern.startswith("/") or "\\" in pattern:
        raise ConfigError(f"{context}.excludes[{index}] must be a relative POSIX glob")

    parts = pattern.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ConfigError(
            f"{context}.excludes[{index}] must not contain empty, `.` or `..` path parts"
        )
    for part in parts:
        bracket_open = False
        for char in part:
            if char == "[":
                if bracket_open:
                    raise ConfigError(
                        f"{context}.excludes[{index}] has an invalid character class"
                    )
                bracket_open = True
            elif char == "]":
                if not bracket_open:
                    raise ConfigError(
                        f"{context}.excludes[{index}] has an invalid character class"
                    )
                bracket_open = False
        if bracket_open:
            raise ConfigError(
                f"{context}.excludes[{index}] has an invalid character class"
            )
    return pattern


def _parse_sync_destination(raw: Any, context: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{context}.destination must be a non-empty string")

    destination = raw.strip()
    first_slash = destination.find("/")
    colon = destination.find(":")
    host_qualified = colon >= 0 and (first_slash < 0 or colon < first_slash)
    if host_qualified and colon == 0:
        raise ConfigError(f"{context}.destination must contain a host before `:`")
    path = destination[colon + 1 :] if host_qualified else destination
    if not path:
        raise ConfigError(f"{context}.destination must contain a path")
    if "\\" in path or "~" in path:
        raise ConfigError(
            f"{context}.destination must be an absolute or relative POSIX path "
            "without `~`"
        )

    trimmed = path.rstrip("/") or "/"
    parts = [] if trimmed == "/" else trimmed.split("/")
    if trimmed.startswith("/") and trimmed != "/":
        parts = parts[1:]
    if any(part in {"", ".", ".."} for part in parts):
        raise ConfigError(
            f"{context}.destination must not contain empty, `.` or `..` path parts"
        )
    return destination


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
    destination = _parse_sync_destination(raw.get("destination"), context)
    if require_paths and source is None:
        raise ConfigError(f"{context}.source is required")
    if require_paths and destination is None:
        raise ConfigError(f"{context}.destination is required")

    excludes_raw = raw.get("excludes", [])
    if not isinstance(excludes_raw, list):
        raise ConfigError(f"{context}.excludes must be a list")
    excludes = [
        _validate_proxy_glob(pattern, i, context)
        for i, pattern in enumerate(excludes_raw)
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
                f"proxy.{target_name}.headers.{header_name} must contain exactly one of `env` or `value`"
            )
        source = next(iter(keys))
        value = header_raw[source]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"proxy.{target_name}.headers.{header_name}.{source} must be a non-empty string"
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
                f"proxy.{target_name}.sync cannot configure mappings when enabled is false"
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


def _parse_proxy_target(name: str, raw: Any) -> ProxyTargetSpec:
    if not NAME_RE.fullmatch(name):
        raise ConfigError(
            f"proxy target name must match {NAME_RE.pattern!r}, got {name!r}"
        )
    if not isinstance(raw, dict):
        raise ConfigError(f"proxy.{name} must be a mapping")

    remote_config: RemoteConfigSpec | None = None
    remote_config_raw = raw.get("config")
    if remote_config_raw is not None:
        if (
            not isinstance(remote_config_raw, str)
            or not remote_config_raw.strip()
            or not remote_config_raw.strip().startswith("/")
        ):
            raise ConfigError(
                f"proxy.{name}.config must be an absolute, non-empty path"
            )
        remote_config = RemoteConfigSpec(path=remote_config_raw.strip())

    transport = raw.get("transport")
    if remote_config is not None:
        if transport is not None:
            raise ConfigError(f"proxy.{name} cannot combine config with transport")
        transport = "remote"
    elif not isinstance(transport, str) or transport not in PROXY_TRANSPORTS:
        raise ConfigError(
            f"proxy.{name}.transport must be one of {sorted(PROXY_TRANSPORTS)}, got {transport!r}"
        )

    command_raw = raw.get("command")
    command: list[str] | None = None
    if command_raw is not None:
        if not isinstance(command_raw, list) or not command_raw:
            raise ConfigError(f"proxy.{name}.command must be a non-empty list of strings")
        if any(not isinstance(part, str) or not part for part in command_raw):
            raise ConfigError(f"proxy.{name}.command must contain only non-empty strings")
        command = command_raw

    cwd = raw.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        raise ConfigError(f"proxy.{name}.cwd must be a non-empty string")

    ssh: SSHSpec | None = None
    ssh_raw = raw.get("ssh")
    if ssh_raw is not None:
        if not isinstance(ssh_raw, dict):
            raise ConfigError(f"proxy.{name}.ssh must be a mapping")
        host = ssh_raw.get("host")
        if not isinstance(host, str) or not host.strip():
            raise ConfigError(f"proxy.{name}.ssh.host must be a non-empty string")
        ssh_command_raw = ssh_raw.get("command")
        if ssh_command_raw is not None and (
            not isinstance(ssh_command_raw, list)
            or not ssh_command_raw
            or any(not isinstance(part, str) or not part for part in ssh_command_raw)
        ):
            raise ConfigError(
                f"proxy.{name}.ssh.command must be a non-empty list of strings"
            )
        ssh = SSHSpec(host=host.strip(), command=ssh_command_raw)

    endpoint = raw.get("endpoint")
    if endpoint is not None and (
        not isinstance(endpoint, str)
        or not endpoint.strip()
        or not endpoint.strip().startswith(("http://", "https://"))
    ):
        raise ConfigError(
            f"proxy.{name}.endpoint must be an http:// or https:// URL"
        )

    if transport == "stdio" and command is None:
        raise ConfigError(f"proxy.{name}.command is required for stdio transport")
    if transport == "ssh" and ssh is None:
        raise ConfigError(f"proxy.{name}.ssh is required for ssh transport")
    if transport == "ssh" and ssh is not None and ssh.command is None:
        raise ConfigError(f"proxy.{name}.ssh.command is required for ssh transport")
    if transport in {"http", "sse"} and endpoint is None:
        raise ConfigError(f"proxy.{name}.endpoint is required for {transport} transport")

    if transport == "ssh" and command is not None:
        raise ConfigError(f"proxy.{name}.command is not used with ssh transport")
    if transport in {"http", "sse"} and command is not None:
        raise ConfigError(f"proxy.{name}.command is not used with {transport} transport")
    if transport == "remote" and ssh is None:
        raise ConfigError(f"proxy.{name}.ssh is required when config is specified")
    if transport == "remote" and ssh is not None and ssh.command is not None:
        raise ConfigError(
            f"proxy.{name}.ssh.command must not be specified when config is specified"
        )
    if transport != "ssh" and transport != "remote" and ssh is not None:
        raise ConfigError(f"proxy.{name}.ssh is only valid for ssh transport")
    if transport not in {"http", "sse"} and endpoint is not None:
        raise ConfigError(f"proxy.{name}.endpoint is only valid for http or sse transport")
    if transport not in {"http", "sse", "remote"} and raw.get("headers") is not None:
        raise ConfigError(f"proxy.{name}.headers is only valid for http or sse transport")
    if transport != "stdio" and cwd is not None:
        raise ConfigError(f"proxy.{name}.cwd is only valid for stdio transport")

    if transport == "remote":
        if command is not None:
            raise ConfigError(f"proxy.{name} cannot combine config with command")
        if endpoint is not None:
            raise ConfigError(f"proxy.{name} cannot combine config with endpoint")
        if cwd is not None:
            raise ConfigError(f"proxy.{name} cannot combine config with cwd")

    sync = _parse_sync(raw.get("sync"), name)
    if transport != "remote" and sync is not None and sync.enabled and any(
        mapping.source is None or mapping.destination is None
        for mapping in sync.mappings
    ):
        raise ConfigError(
            f"proxy.{name}.sync.source and sync.destination are required for explicit transports"
        )

    return ProxyTargetSpec(
        name=name,
        transport=transport,
        command=command,
        cwd=cwd.strip() if isinstance(cwd, str) else None,
        ssh=ssh,
        endpoint=endpoint.strip() if isinstance(endpoint, str) else None,
        headers=_parse_headers(raw.get("headers"), name),
        sync=sync,
        remote_config=remote_config,
    )


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


def _parse_tls(raw: Any) -> TLSConfig:
    if not isinstance(raw, dict):
        raise ConfigError("server.tls must be a mapping")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("server.tls.enabled must be a boolean")

    auto_generate = raw.get("auto_generate", False)
    if not isinstance(auto_generate, bool):
        raise ConfigError("server.tls.auto_generate must be a boolean")

    cert_file = raw.get("cert_file")
    if cert_file is not None and (
        not isinstance(cert_file, str) or not cert_file.strip()
    ):
        raise ConfigError("server.tls.cert_file must be a non-empty string")

    key_file = raw.get("key_file")
    if key_file is not None and (
        not isinstance(key_file, str) or not key_file.strip()
    ):
        raise ConfigError("server.tls.key_file must be a non-empty string")

    hostnames_raw = raw.get("hostnames", [])
    if not isinstance(hostnames_raw, list):
        raise ConfigError("server.tls.hostnames must be a list")
    hostnames: list[str] = []
    for i, hostname in enumerate(hostnames_raw):
        if not isinstance(hostname, str) or not hostname.strip():
            raise ConfigError(
                f"server.tls.hostnames[{i}] must be a non-empty string"
            )
        hostnames.append(hostname.strip())

    if enabled and ((cert_file is None) != (key_file is None)):
        raise ConfigError(
            "server.tls.cert_file and server.tls.key_file must be provided together"
        )
    if enabled and cert_file is None and key_file is None and not auto_generate:
        raise ConfigError(
            "server.tls requires cert_file/key_file or auto_generate: true"
        )

    return TLSConfig(
        enabled=enabled,
        cert_file=cert_file,
        key_file=key_file,
        auto_generate=auto_generate,
        hostnames=hostnames,
    )


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}")
    if not isinstance(raw, dict):
        raise ConfigError(f"top-level YAML must be a mapping in {path}")
    if "webdav" in raw:
        raise ConfigError("`webdav` is no longer supported")

    if "mode" in raw:
        raise ConfigError(
            "`mode` is no longer supported; configure `commands` and/or `proxy`"
        )

    server_raw = raw.get("server") or {}
    if not isinstance(server_raw, dict):
        raise ConfigError("`server` must be a mapping")
    server_transport = server_raw.get("transport", "http")
    if not isinstance(server_transport, str) or server_transport not in SERVER_TRANSPORTS:
        raise ConfigError(
            f"server.transport must be one of {sorted(SERVER_TRANSPORTS)}, got {server_transport!r}"
        )
    server = ServerSpec(
        host=server_raw.get("host"),
        port=int(server_raw["port"]) if server_raw.get("port") is not None else None,
        transport=server_transport,
        public_base_url=server_raw.get("public_base_url"),
        tls=_parse_tls(server_raw.get("tls") or {}),
    )

    auth_raw = raw.get("auth") or {}
    if not isinstance(auth_raw, dict):
        raise ConfigError("`auth` must be a mapping")
    auth = AuthSpec(api_key=auth_raw.get("api_key"))

    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise ConfigError("`defaults` must be a mapping")
    defaults = DefaultsSpec(
        timeout=float(defaults_raw["timeout"])
        if defaults_raw.get("timeout") is not None
        else None,
        cwd=defaults_raw.get("cwd"),
    )

    commands_raw = raw.get("commands", [])
    if not isinstance(commands_raw, list):
        raise ConfigError("`commands` must be a list")
    commands = [_parse_command(c) for c in commands_raw]

    seen: set[str] = set()
    for cmd in commands:
        if cmd.name in seen:
            raise ConfigError(f"duplicate command name: {cmd.name!r}")
        seen.add(cmd.name)

    proxy = _parse_proxy(raw["proxy"]) if "proxy" in raw else None
    if not commands and proxy is None:
        raise ConfigError("at least one of `commands` or `proxy` must be configured")

    return Config(
        server=server,
        auth=auth,
        defaults=defaults,
        commands=commands,
        proxy=proxy,
        config_path=path,
    )
