"""Compatibility tests for the public :mod:`rcm.config` facade."""

from __future__ import annotations

import pickle
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import rcm.config as config_module
from rcm.config import (
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
    load_config,
)


PUBLIC_MODELS = {
    "AuthSpec": AuthSpec,
    "CollectPathSpec": CollectPathSpec,
    "CollectSpec": CollectSpec,
    "CommandSpec": CommandSpec,
    "Config": Config,
    "ConfigError": ConfigError,
    "DefaultsSpec": DefaultsSpec,
    "HeaderSpec": HeaderSpec,
    "ParamSpec": ParamSpec,
    "ProxySpec": ProxySpec,
    "ProxyTargetSpec": ProxyTargetSpec,
    "RemoteConfigSpec": RemoteConfigSpec,
    "SSHSpec": SSHSpec,
    "ServerSpec": ServerSpec,
    "SyncMappingSpec": SyncMappingSpec,
    "SyncSpec": SyncSpec,
    "TLSConfig": TLSConfig,
}


def test_public_config_imports_remain_available() -> None:
    for name, expected in PUBLIC_MODELS.items():
        assert getattr(config_module, name) is expected
        assert expected.__module__ == "rcm.config"
    assert config_module.load_config is load_config
    assert load_config.__module__ == "rcm.config"
    assert config_module.ARTIFACT_MODES == {"localize", "passthrough"}
    assert config_module.PROXY_TRANSPORTS == {"stdio", "ssh", "http", "sse"}


def test_public_model_pickle_path_remains_compatible() -> None:
    original = ProxyTargetSpec(name="tools", transport="stdio", command=["tool"])
    restored = pickle.loads(pickle.dumps(original))
    assert restored == original
    assert type(restored) is ProxyTargetSpec


def test_public_model_defaults_and_legacy_sync_constructor() -> None:
    mapping = SyncMappingSpec(source="./src", destination="dst")
    target = ProxyTargetSpec(name="remote", transport="stdio", command=["tool"])
    config = Config(
        server=ServerSpec(),
        auth=AuthSpec(),
        defaults=DefaultsSpec(),
        commands=[CommandSpec("echo", "Echo", ["echo"])],
        proxy=ProxySpec(targets=[target]),
    )

    assert config.server.tls == TLSConfig()
    assert target.headers == {}
    assert SyncSpec(mappings=[mapping]).mappings == [mapping]

    legacy = SyncSpec(
        source="./src",
        destination="dst",
        excludes=["build/**"],
        delete=True,
    )
    assert legacy.source == "./src"
    assert legacy.destination == "dst"
    assert legacy.excludes == ["build/**"]
    assert legacy.delete is True


def test_existing_mutability_contract_is_preserved() -> None:
    param = ParamSpec(name="value", type="string")
    param.description = "changed"
    assert param.description == "changed"

    collect = CollectSpec(paths=(CollectPathSpec(path="dist"),))
    with pytest.raises(FrozenInstanceError):
        collect.mode = "changed"  # type: ignore[misc]


def test_unknown_fields_remain_ignored_outside_strict_collect_sections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "commands.yaml"
    path.write_text(
        """
unknown_top_level: retained-for-compatibility
server:
  unknown_server_field: ignored
commands:
  - name: echo
    description: Echo.
    command: [echo]
    unknown_command_field: ignored
proxy:
  tools:
    transport: http
    endpoint: https://example.com/mcp
    unknown_target_field: ignored
""".strip(),
        encoding="utf-8",
    )

    config = load_config(path)
    assert [command.name for command in config.commands] == ["echo"]
    assert config.proxy is not None
    assert [target.name for target in config.proxy.targets] == ["tools"]


def test_less_common_public_models_keep_their_constructor_shapes() -> None:
    assert HeaderSpec(env="TOKEN").env == "TOKEN"
    assert SSHSpec(host="host", command=["rcm"]).command == ["rcm"]
    assert RemoteConfigSpec(path="/etc/rcm.yaml").path == "/etc/rcm.yaml"
