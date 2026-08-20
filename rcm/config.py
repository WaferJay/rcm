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
WEBDAV_AUTH_TYPES = {"basic", "bearer", "none"}
WEBDAV_RESERVED_PATHS = ("/mcp", "/runs", "/healthz")


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
class WebDAVAuthSpec:
    type: str = "bearer"
    username: str | None = None
    password: str | None = None


@dataclass
class WebDAVSpec:
    root: str
    path: str = "/webdav/"
    read_only: bool = True
    auth: WebDAVAuthSpec = field(default_factory=WebDAVAuthSpec)


@dataclass
class Config:
    server: ServerSpec
    auth: AuthSpec
    defaults: DefaultsSpec
    commands: list[CommandSpec]
    webdav: WebDAVSpec | None = None


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


def _normalize_webdav_path(path: Any) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ConfigError("webdav.path must be a non-empty string")

    normalized = "/" + path.strip().strip("/")
    if normalized == "/":
        raise ConfigError("webdav.path must not be `/`")
    if any(
        normalized == reserved or normalized.startswith(reserved + "/")
        for reserved in WEBDAV_RESERVED_PATHS
    ):
        raise ConfigError(
            f"webdav.path {path!r} conflicts with a reserved path; "
            f"reserved paths are {list(WEBDAV_RESERVED_PATHS)!r}"
        )
    return normalized + "/"


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


def _parse_webdav(raw: Any) -> WebDAVSpec:
    if not isinstance(raw, dict):
        raise ConfigError("`webdav` must be a mapping")

    root = raw.get("root")
    if not isinstance(root, str) or not root.strip():
        raise ConfigError("webdav.root must be a non-empty string")

    read_only = raw.get("read_only", True)
    if not isinstance(read_only, bool):
        raise ConfigError("webdav.read_only must be a boolean")

    auth_raw = raw.get("auth")
    if auth_raw is None:
        auth_raw = {}
    if not isinstance(auth_raw, dict):
        raise ConfigError("webdav.auth must be a mapping")
    auth_type = auth_raw.get("type", "bearer")
    if not isinstance(auth_type, str) or auth_type not in WEBDAV_AUTH_TYPES:
        raise ConfigError(
            "webdav.auth.type must be one of "
            f"{sorted(WEBDAV_AUTH_TYPES)}, got {auth_type!r}"
        )

    username = auth_raw.get("username")
    password = auth_raw.get("password")
    if username is not None and (not isinstance(username, str) or not username):
        raise ConfigError("webdav.auth.username must be a non-empty string")
    if password is not None and (not isinstance(password, str) or not password):
        raise ConfigError("webdav.auth.password must be a non-empty string")
    if auth_type == "basic" and (username is None or password is None):
        raise ConfigError(
            "webdav.auth.username and webdav.auth.password are required for basic auth"
        )

    return WebDAVSpec(
        root=root,
        path=_normalize_webdav_path(raw.get("path", "/webdav/")),
        read_only=read_only,
        auth=WebDAVAuthSpec(
            type=auth_type,
            username=username,
            password=password,
        ),
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}")
    if not isinstance(raw, dict):
        raise ConfigError(f"top-level YAML must be a mapping in {path}")

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

    commands_raw = raw.get("commands")
    if not isinstance(commands_raw, list) or not commands_raw:
        raise ConfigError("`commands` must be a non-empty list")
    commands = [_parse_command(c) for c in commands_raw]

    seen: set[str] = set()
    for cmd in commands:
        if cmd.name in seen:
            raise ConfigError(f"duplicate command name: {cmd.name!r}")
        seen.add(cmd.name)

    webdav = _parse_webdav(raw["webdav"]) if "webdav" in raw else None

    return Config(
        server=server,
        auth=auth,
        defaults=defaults,
        commands=commands,
        webdav=webdav,
    )
