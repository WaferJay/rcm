"""Pure parsers that turn decoded YAML mappings into config models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .commands import _parse_command
from .models import (
    SERVER_TRANSPORTS,
    AuthSpec,
    Config,
    ConfigError,
    DefaultsSpec,
    ServerSpec,
    TLSConfig,
)
from .proxy import _parse_proxy


@dataclass(frozen=True)
class RuntimeSectionMappings:
    """Decoded runtime sections shared by local and remote config readers."""

    server: dict[str, Any]
    auth: dict[str, Any]
    defaults: dict[str, Any]
    transport: str


def _runtime_section(
    raw: dict[str, Any],
    name: str,
    *,
    error_prefix: str,
    quote_names: bool,
) -> dict[str, Any]:
    value = raw.get(name) or {}
    if not isinstance(value, dict):
        display = f"`{name}`" if quote_names else name
        raise ConfigError(f"{error_prefix}{display} must be a mapping")
    return value


def parse_runtime_sections(
    raw: dict[str, Any],
    *,
    error_prefix: str = "",
    quote_names: bool = True,
    transport_choices: str | None = None,
) -> RuntimeSectionMappings:
    """Parse the common server/auth/defaults section envelopes."""
    server = _runtime_section(
        raw,
        "server",
        error_prefix=error_prefix,
        quote_names=quote_names,
    )
    transport = server.get("transport", "http")
    if not isinstance(transport, str) or transport not in SERVER_TRANSPORTS:
        choices = transport_choices or f"one of {sorted(SERVER_TRANSPORTS)}"
        raise ConfigError(
            f"{error_prefix}server.transport must be {choices}, got {transport!r}"
        )
    auth = _runtime_section(
        raw,
        "auth",
        error_prefix=error_prefix,
        quote_names=quote_names,
    )
    defaults = _runtime_section(
        raw,
        "defaults",
        error_prefix=error_prefix,
        quote_names=quote_names,
    )
    return RuntimeSectionMappings(
        server=server,
        auth=auth,
        defaults=defaults,
        transport=transport,
    )

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
    for index, hostname in enumerate(hostnames_raw):
        if not isinstance(hostname, str) or not hostname.strip():
            raise ConfigError(
                f"server.tls.hostnames[{index}] must be a non-empty string"
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


def parse_config_document(raw: dict[str, Any], path: Path) -> Config:
    """Build a complete config from an already-decoded top-level mapping."""
    if "webdav" in raw:
        raise ConfigError("`webdav` is no longer supported")
    if "mode" in raw:
        raise ConfigError(
            "`mode` is no longer supported; configure `commands` and/or `proxy`"
        )

    runtime = parse_runtime_sections(raw)
    server = ServerSpec(
        host=runtime.server.get("host"),
        port=(
            int(runtime.server["port"])
            if runtime.server.get("port") is not None
            else None
        ),
        transport=runtime.transport,
        public_base_url=runtime.server.get("public_base_url"),
        tls=_parse_tls(runtime.server.get("tls") or {}),
    )
    auth = AuthSpec(api_key=runtime.auth.get("api_key"))
    defaults = DefaultsSpec(
        timeout=(
            float(runtime.defaults["timeout"])
            if runtime.defaults.get("timeout") is not None
            else None
        ),
        cwd=runtime.defaults.get("cwd"),
    )

    commands_raw = raw.get("commands", [])
    if not isinstance(commands_raw, list):
        raise ConfigError("`commands` must be a list")
    commands = [_parse_command(command) for command in commands_raw]

    seen: set[str] = set()
    for command in commands:
        if command.name in seen:
            raise ConfigError(f"duplicate command name: {command.name!r}")
        seen.add(command.name)

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
