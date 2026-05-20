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
class ServerSpec:
    host: str | None = None
    port: int | None = None
    public_base_url: str | None = None


@dataclass
class AuthSpec:
    api_key: str | None = None


@dataclass
class DefaultsSpec:
    timeout: float | None = None
    cwd: str | None = None


@dataclass
class Config:
    server: ServerSpec
    auth: AuthSpec
    defaults: DefaultsSpec
    commands: list[CommandSpec]


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

    return Config(server=server, auth=auth, defaults=defaults, commands=commands)
