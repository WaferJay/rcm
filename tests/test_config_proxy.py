"""Tests for proxy-target configuration parsing."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from rcm.config import ConfigError, load_config


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "commands.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_load_proxy_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMOTE_MCP_AUTH", "Bearer env-token")
    cfg = load_config(
        write(
            tmp_path,
            """
            proxy:
              compile:
                transport: ssh
                ssh:
                  host: compile-machine
                  command: [rcm, --stdio]
                sync:
                  source: /local/project
                  destination: /remote/project
                  excludes: [".git/**", "**/*.pyc"]
                  delete: false
              tools:
                transport: http
                endpoint: https://example.com/mcp
                artifacts: passthrough
                headers:
                  Authorization: {env: REMOTE_MCP_AUTH}
                  X-Project: {value: compile}
            """,
        )
    )

    assert cfg.commands == []
    assert cfg.proxy is not None
    compile_target, http_target = cfg.proxy.targets
    assert compile_target.ssh is not None
    assert compile_target.ssh.host == "compile-machine"
    assert compile_target.sync is not None
    assert compile_target.sync.enabled is True
    assert len(compile_target.sync.mappings) == 1
    mapping = compile_target.sync.mappings[0]
    assert mapping.source == "/local/project"
    assert mapping.destination == "/remote/project"
    assert mapping.excludes == [".git/**", "**/*.pyc"]
    assert mapping.delete is False
    assert http_target.headers["Authorization"].env == "REMOTE_MCP_AUTH"
    assert http_target.headers["X-Project"].value == "compile"
    assert http_target.artifacts == "passthrough"
    assert compile_target.artifacts is None


def test_load_multi_mapping_sync_config(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            proxy:
              compile:
                transport: ssh
                ssh:
                  host: compile-machine
                  command: [rcm, --stdio]
                sync:
                  mappings:
                    - source: ./backend
                      destination: backend
                      excludes: ["**/*.pyc"]
                    - source: ./shared
                      destination: /srv/project/shared
                      delete: true
            """,
        )
    )

    sync = cfg.proxy.targets[0].sync
    assert sync is not None
    assert sync.enabled is True
    assert len(sync.mappings) == 2
    assert sync.mappings[0].source == "./backend"
    assert sync.mappings[0].destination == "backend"
    assert sync.mappings[0].excludes == ["**/*.pyc"]
    assert sync.mappings[0].delete is False
    assert sync.mappings[1].source == "./shared"
    assert sync.mappings[1].destination == "/srv/project/shared"
    assert sync.mappings[1].delete is True


def test_load_commands_and_proxy_together(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            commands:
              - name: local_echo
                description: Echo locally.
                command: [echo, local]
            proxy:
              remote:
                transport: http
                endpoint: https://example.com/mcp
            """,
        )
    )
    assert [command.name for command in cfg.commands] == ["local_echo"]
    assert cfg.proxy is not None
    assert [target.name for target in cfg.proxy.targets] == ["remote"]


def test_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mode.*no longer supported"):
        load_config(
            write(
                tmp_path,
                """
                mode: proxy
                commands: []
                proxy:
                  remote:
                    transport: http
                    endpoint: https://example.com/mcp
                """,
            )
        )


def test_load_remote_config_proxy_target(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            proxy:
              compile:
                ssh:
                  host: compile-machine
                config: /etc/rcm/commands.yaml
                artifacts: passthrough
                sync:
                  enabled: false
            """,
        )
    )
    target = cfg.proxy.targets[0]
    assert target.transport == "remote"
    assert target.remote_config is not None
    assert target.remote_config.path == "/etc/rcm/commands.yaml"
    assert target.ssh is not None
    assert target.ssh.command is None
    assert target.ssh.tunnel is False
    assert target.sync is not None
    assert target.sync.enabled is False
    assert target.artifacts == "passthrough"


def test_load_remote_config_ssh_tunnel(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            proxy:
              compile:
                ssh:
                  host: compile-machine
                  tunnel: true
                config: /etc/rcm/commands.yaml
                artifacts: localize
                sync: {enabled: false}
            """,
        )
    )

    target = cfg.proxy.targets[0]
    assert target.ssh is not None
    assert target.ssh.tunnel is True
    assert target.artifacts == "localize"


@pytest.mark.parametrize(
    "body, fragment",
    [
        (
            """
            proxy:
              target:
                ssh: {host: compile-machine, tunnel: yes-please}
                config: /etc/rcm/commands.yaml
            """,
            "ssh.tunnel must be a boolean",
        ),
        (
            """
            proxy:
              target:
                transport: ssh
                ssh:
                  host: compile-machine
                  command: [rcm, --stdio]
                  tunnel: true
            """,
            "only valid when config is specified",
        ),
        (
            """
            proxy:
              target:
                ssh: {host: compile-machine, tunnel: true}
                config: /etc/rcm/commands.yaml
                artifacts: passthrough
            """,
            "passthrough is not supported with an SSH tunnel",
        ),
    ],
)
def test_invalid_ssh_tunnel_configs_rejected(
    tmp_path: Path,
    body: str,
    fragment: str,
) -> None:
    with pytest.raises(ConfigError, match=fragment):
        load_config(write(tmp_path, body))


@pytest.mark.parametrize(
    "body, fragment",
    [
        (
            """
            proxy:
              target:
                ssh: {host: compile-machine}
                config: relative/commands.yaml
            """,
            "absolute",
        ),
        (
            """
            proxy:
              target:
                transport: http
                ssh: {host: compile-machine}
                config: /etc/rcm/commands.yaml
            """,
            "cannot combine config with transport",
        ),
        (
            """
            proxy:
              target:
                ssh:
                  host: compile-machine
                  command: [rcm, --stdio]
                config: /etc/rcm/commands.yaml
            """,
            "must not be specified",
        ),
        (
            """
            proxy:
              target:
                config: /etc/rcm/commands.yaml
            """,
            "ssh is required",
        ),
        (
            """
            proxy:
              sync:
                enabled: true
            """,
            "proxy.sync` is not a top-level setting",
        ),
    ],
)
def test_invalid_remote_config_proxy_targets_rejected(
    tmp_path: Path, body: str, fragment: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(
            write(
                tmp_path,
                "server: {public_base_url: http://x}\n" + textwrap.dedent(body),
            )
        )
    assert fragment in str(exc.value)


def test_sync_enabled_false_is_parsed(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            proxy:
              local:
                transport: stdio
                command: [echo]
                sync:
                  enabled: false
            """,
        )
    )
    assert cfg.proxy.targets[0].sync is not None
    assert cfg.proxy.targets[0].sync.enabled is False
    assert cfg.proxy.targets[0].sync.mappings == []


@pytest.mark.parametrize(
    "sync_body, fragment",
    [
        (
            """
            source: ./legacy
            destination: legacy
            mappings:
              - source: ./new
                destination: new
            """,
            "cannot combine `mappings`",
        ),
        ("mappings: []", "mappings must be a non-empty list"),
        (
            """
            enabled: false
            mappings:
              - source: ./src
                destination: dst
            """,
            "cannot configure mappings when enabled is false",
        ),
        (
            """
            mappings:
              - destination: dst
            """,
            "mappings[0].source is required",
        ),
        (
            """
            mappings:
              - source: ./src
            """,
            "mappings[0].destination is required",
        ),
        (
            """
            mappings:
              - source: ./src
                destination: ../escape
            """,
            "must not contain empty, `.` or `..` path parts",
        ),
        (
            """
            mappings:
              - source: ./src
                destination: ./nested
            """,
            "must not contain empty, `.` or `..` path parts",
        ),
        (
            """
            mappings:
              - source: ./src
                destination: ~/nested
            """,
            "without `~`",
        ),
        (
            """
            mappings:
              - source: ./src
                destination: :nested
            """,
            "must contain a host before `:`",
        ),
    ],
)
def test_invalid_sync_mappings_rejected(
    tmp_path: Path, sync_body: str, fragment: str
) -> None:
    body = """
        proxy:
          target:
            transport: ssh
            ssh:
              host: compile-machine
              command: [rcm, --stdio]
            sync:
    """ + textwrap.indent(textwrap.dedent(sync_body), " " * 14)
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, body))
    assert fragment in str(exc.value)


def test_webdav_config_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(
            write(
                tmp_path,
                """
                server: {public_base_url: http://x}
                webdav: {root: /tmp}
                commands:
                  - name: hello
                    description: Say hello.
                    command: ["echo", "hi"]
                """,
            )
        )
    assert "webdav" in str(exc.value)


@pytest.mark.parametrize(
    "body, fragment",
    [
        (
            """
            commands: []
            """,
            "at least one of `commands` or `proxy`",
        ),
        (
            """
            commands: []
            proxy:
              target:
                transport: http
                endpoint: https://example.com/mcp
                headers:
                  Authorization: {env: TOKEN, value: plaintext}
            """,
            "must contain exactly one of",
        ),
        (
            """
            commands: []
            proxy:
              target:
                transport: stdio
                command: [echo]
                sync:
                  source: /tmp/src
                  destination: /tmp/dst
                  excludes: [../secret]
            """,
            "must not contain empty, `.` or `..` path parts",
        ),
        (
            """
            commands: []
            proxy:
              target:
                transport: http
            """,
            "endpoint is required",
        ),
        (
            """
            proxy:
              target:
                transport: http
                endpoint: https://example.com/mcp
                artifacts: copy
            """,
            "artifacts must be one of",
        ),
        (
            """
            proxy:
              target:
                transport: ssh
                ssh:
                  host: compile-machine
                  command: [rcm, --stdio]
                artifacts: passthrough
            """,
            "passthrough requires an HTTP RCM target",
        ),
    ],
)
def test_invalid_proxy_configs_rejected(
    tmp_path: Path, body: str, fragment: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(
            write(
                tmp_path,
                "server: {public_base_url: http://x}\n" + textwrap.dedent(body),
            )
        )
    assert fragment in str(exc.value)
