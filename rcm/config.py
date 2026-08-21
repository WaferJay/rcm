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
MODES = {"server", "proxy"}
PROXY_TRANSPORTS = {"stdio", "ssh", "http", "sse"}


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
class SyncSpec:
    source: str
    destination: str
    excludes: list[str] = field(default_factory=list)
    delete: bool = False


@dataclass
class SSHSpec:
    host: str
    command: list[str]


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


@dataclass
class ProxySpec:
    targets: list[ProxyTargetSpec] = field(default_factory=list)


@dataclass
class Config:
    server: ServerSpec
    auth: AuthSpec
    defaults: DefaultsSpec
    commands: list[CommandSpec]
    mode: str = "server"
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


def _validate_proxy_glob(pattern: Any, index: int) -> str:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ConfigError(f"proxy.sync.excludes[{index}] must be a non-empty string")
    pattern = pattern.strip()
    if pattern.startswith("/") or "\\" in pattern:
        raise ConfigError(
            f"proxy.sync.excludes[{index}] must be a relative POSIX glob"
        )

    parts = pattern.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ConfigError(
            f"proxy.sync.excludes[{index}] must not contain empty, `.` or `..` path parts"
        )
    for part in parts:
        bracket_open = False
        for char in part:
            if char == "[":
                if bracket_open:
                    raise ConfigError(
                        f"proxy.sync.excludes[{index}] has an invalid character class"
                    )
                bracket_open = True
            elif char == "]":
                if not bracket_open:
                    raise ConfigError(
                        f"proxy.sync.excludes[{index}] has an invalid character class"
                    )
                bracket_open = False
        if bracket_open:
            raise ConfigError(
                f"proxy.sync.excludes[{index}] has an invalid character class"
            )
    return pattern


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

    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ConfigError(f"proxy.{target_name}.sync.source must be a non-empty string")
    destination = raw.get("destination")
    if not isinstance(destination, str) or not destination.strip():
        raise ConfigError(
            f"proxy.{target_name}.sync.destination must be a non-empty string"
        )
    excludes_raw = raw.get("excludes", [])
    if not isinstance(excludes_raw, list):
        raise ConfigError(f"proxy.{target_name}.sync.excludes must be a list")
    excludes = [_validate_proxy_glob(pattern, i) for i, pattern in enumerate(excludes_raw)]

    delete = raw.get("delete", False)
    if not isinstance(delete, bool):
        raise ConfigError(f"proxy.{target_name}.sync.delete must be a boolean")
    return SyncSpec(
        source=source.strip(),
        destination=destination.strip(),
        excludes=excludes,
        delete=delete,
    )


def _parse_proxy_target(name: str, raw: Any) -> ProxyTargetSpec:
    if not NAME_RE.fullmatch(name):
        raise ConfigError(
            f"proxy target name must match {NAME_RE.pattern!r}, got {name!r}"
        )
    if not isinstance(raw, dict):
        raise ConfigError(f"proxy.{name} must be a mapping")

    transport = raw.get("transport")
    if not isinstance(transport, str) or transport not in PROXY_TRANSPORTS:
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
        if (
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
    if transport in {"http", "sse"} and endpoint is None:
        raise ConfigError(f"proxy.{name}.endpoint is required for {transport} transport")

    if transport == "ssh" and command is not None:
        raise ConfigError(f"proxy.{name}.command is not used with ssh transport")
    if transport in {"http", "sse"} and command is not None:
        raise ConfigError(f"proxy.{name}.command is not used with {transport} transport")
    if transport != "ssh" and ssh is not None:
        raise ConfigError(f"proxy.{name}.ssh is only valid for ssh transport")
    if transport not in {"http", "sse"} and endpoint is not None:
        raise ConfigError(f"proxy.{name}.endpoint is only valid for http or sse transport")
    if transport not in {"http", "sse"} and raw.get("headers") is not None:
        raise ConfigError(f"proxy.{name}.headers is only valid for http or sse transport")
    if transport != "stdio" and cwd is not None:
        raise ConfigError(f"proxy.{name}.cwd is only valid for stdio transport")

    return ProxyTargetSpec(
        name=name,
        transport=transport,
        command=command,
        cwd=cwd.strip() if isinstance(cwd, str) else None,
        ssh=ssh,
        endpoint=endpoint.strip() if isinstance(endpoint, str) else None,
        headers=_parse_headers(raw.get("headers"), name),
        sync=_parse_sync(raw.get("sync"), name),
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

    mode = raw.get("mode", "server")
    if not isinstance(mode, str) or mode not in MODES:
        raise ConfigError(f"`mode` must be one of {sorted(MODES)}, got {mode!r}")

    server_raw = raw.get("server") or {}
    if not isinstance(server_raw, dict):
        raise ConfigError("`server` must be a mapping")
    server = ServerSpec(
        host=server_raw.get("host"),
        port=int(server_raw["port"]) if server_raw.get("port") is not None else None,
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
    if not isinstance(commands_raw, list) or (mode == "server" and not commands_raw):
        raise ConfigError("`commands` must be a non-empty list")
    commands = [_parse_command(c) for c in commands_raw]

    seen: set[str] = set()
    for cmd in commands:
        if cmd.name in seen:
            raise ConfigError(f"duplicate command name: {cmd.name!r}")
        seen.add(cmd.name)

    proxy = _parse_proxy(raw["proxy"]) if "proxy" in raw else None
    if mode == "proxy" and proxy is None:
        raise ConfigError("`proxy` is required when mode is `proxy`")
    if mode == "server" and proxy is not None:
        raise ConfigError("`proxy` is only valid when mode is `proxy`")

    return Config(
        server=server,
        auth=auth,
        defaults=defaults,
        commands=commands,
        mode=mode,
        proxy=proxy,
        config_path=path,
    )
