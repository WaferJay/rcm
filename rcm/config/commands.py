"""Parsing for command, parameter, and collection sections."""

from __future__ import annotations

import re
import string
from typing import Any

from .models import (
    COLLECT_MODES,
    COLLECT_ON_EXIT,
    NAME_RE,
    PARAM_TYPES,
    CollectPathSpec,
    CollectSpec,
    CommandSpec,
    ConfigError,
    ParamSpec,
)


def _placeholders(value: str) -> list[str]:
    """Return the ``{name}`` placeholders in a Python format string."""
    out: list[str] = []
    for _, field_name, _, _ in string.Formatter().parse(value):
        if field_name is None:
            continue
        if not field_name:
            raise ConfigError(f"empty placeholder in {value!r}")
        if "." in field_name or "[" in field_name:
            raise ConfigError(f"unsupported placeholder syntax in {value!r}")
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
        except re.error as exc:
            raise ConfigError(
                f"params.{name}.pattern is not a valid regex: {exc}"
            ) from exc
        spec.pattern = pattern
    if "enum" in raw:
        enum = raw["enum"]
        if not isinstance(enum, list) or not enum:
            raise ConfigError(f"params.{name}.enum must be a non-empty list")
        spec.enum = enum
    return spec


def _parse_collect(raw: Any, command_name: str) -> CollectSpec | None:
    if raw is None:
        return None
    context = f"command {command_name!r}: collect"
    if not isinstance(raw, dict):
        raise ConfigError(f"{context} must be a mapping")
    unknown = set(raw) - {"paths", "on_exit", "mode"}
    if unknown:
        raise ConfigError(f"{context} has unknown fields: {sorted(unknown)}")

    on_exit = raw.get("on_exit", "success")
    if not isinstance(on_exit, str) or on_exit not in COLLECT_ON_EXIT:
        raise ConfigError(
            f"{context}.on_exit must be one of {sorted(COLLECT_ON_EXIT)}, "
            f"got {on_exit!r}"
        )
    mode = raw.get("mode", "always")
    if not isinstance(mode, str) or mode not in COLLECT_MODES:
        raise ConfigError(
            f"{context}.mode must be one of {sorted(COLLECT_MODES)}, got {mode!r}"
        )

    paths_raw = raw.get("paths")
    if not isinstance(paths_raw, list) or not paths_raw:
        raise ConfigError(f"{context}.paths must be a non-empty list")

    paths: list[CollectPathSpec] = []
    for index, item in enumerate(paths_raw):
        item_context = f"{context}.paths[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{item_context} must be a mapping")
        unknown = set(item) - {"path", "required", "on_exit", "mode"}
        if unknown:
            raise ConfigError(
                f"{item_context} has unknown fields: {sorted(unknown)}"
            )
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ConfigError(f"{item_context}.path must be a non-empty string")
        path = path.strip()
        if "\x00" in path:
            raise ConfigError(f"{item_context}.path must not contain a NUL byte")
        if path.startswith("/") or "\\" in path:
            raise ConfigError(f"{item_context}.path must be a relative POSIX path")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ConfigError(
                f"{item_context}.path must not contain empty, `.` or `..` path parts"
            )
        if any("**" in part and part != "**" for part in parts):
            raise ConfigError(
                f"{item_context}.path only supports `**` as a complete path part"
            )
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise ConfigError(f"{item_context}.required must be a boolean")
        path_on_exit = item.get("on_exit")
        if path_on_exit is not None and (
            not isinstance(path_on_exit, str) or path_on_exit not in COLLECT_ON_EXIT
        ):
            raise ConfigError(
                f"{item_context}.on_exit must be one of "
                f"{sorted(COLLECT_ON_EXIT)}, got {path_on_exit!r}"
            )
        path_mode = item.get("mode")
        if path_mode is not None and (
            not isinstance(path_mode, str) or path_mode not in COLLECT_MODES
        ):
            raise ConfigError(
                f"{item_context}.mode must be one of {sorted(COLLECT_MODES)}, "
                f"got {path_mode!r}"
            )
        paths.append(
            CollectPathSpec(
                path=path,
                required=required,
                on_exit=path_on_exit,
                mode=path_mode,
            )
        )

    return CollectSpec(paths=tuple(paths), on_exit=on_exit, mode=mode)


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
            f"command {name!r}: `command` must be a non-empty list of strings "
            "(argv form)"
        )
    for index, part in enumerate(command):
        if not isinstance(part, str):
            raise ConfigError(
                f"command {name!r}: command[{index}] must be a string, "
                f"got {type(part).__name__}"
            )

    params_raw = raw.get("params") or {}
    if not isinstance(params_raw, dict):
        raise ConfigError(f"command {name!r}: `params` must be a mapping")
    params = [_parse_param(pname, pspec) for pname, pspec in params_raw.items()]
    declared = {param.name for param in params}

    used: set[str] = set()
    for part in command:
        for placeholder in _placeholders(part):
            if placeholder not in declared:
                raise ConfigError(
                    f"command {name!r}: placeholder {{{placeholder}}} "
                    "has no matching params entry"
                )
            used.add(placeholder)
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
        collect=_parse_collect(raw.get("collect"), name),
    )
