"""YAML configuration models, loader, and validation for rcm."""

from ..artifacts import ARTIFACT_MODES
from .loader import load_config
from .models import (
    COLLECT_MODES,
    COLLECT_ON_EXIT,
    NAME_RE,
    PARAM_TYPES,
    PROXY_TRANSPORTS,
    SERVER_TRANSPORTS,
    AuthSpec,
    CollectPathSpec,
    CollectSpec,
    CommandSpec,
    Config,
    ConfigError,
    DefaultsSpec,
    HeaderSpec,
    ParamSpec,
    ProxySpec,
    ProxyTargetSpec,
    RemoteConfigSpec,
    SSHSpec,
    ServerSpec,
    SyncMappingSpec,
    SyncSpec,
    TLSConfig,
)

# Keep introspection and pickle paths compatible with the former single module.
_PUBLIC_TYPES = (
    AuthSpec,
    CollectPathSpec,
    CollectSpec,
    CommandSpec,
    Config,
    ConfigError,
    DefaultsSpec,
    HeaderSpec,
    ParamSpec,
    ProxySpec,
    ProxyTargetSpec,
    RemoteConfigSpec,
    SSHSpec,
    ServerSpec,
    SyncMappingSpec,
    SyncSpec,
    TLSConfig,
)
for _public_type in _PUBLIC_TYPES:
    _public_type.__module__ = __name__
load_config.__module__ = __name__
del _public_type, _PUBLIC_TYPES

__all__ = [
    "ARTIFACT_MODES",
    "COLLECT_MODES",
    "COLLECT_ON_EXIT",
    "NAME_RE",
    "PARAM_TYPES",
    "PROXY_TRANSPORTS",
    "SERVER_TRANSPORTS",
    "AuthSpec",
    "CollectPathSpec",
    "CollectSpec",
    "CommandSpec",
    "Config",
    "ConfigError",
    "DefaultsSpec",
    "HeaderSpec",
    "ParamSpec",
    "ProxySpec",
    "ProxyTargetSpec",
    "RemoteConfigSpec",
    "SSHSpec",
    "ServerSpec",
    "SyncMappingSpec",
    "SyncSpec",
    "TLSConfig",
    "load_config",
]
