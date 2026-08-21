"""Tests for rcm.config: YAML loading and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from rcm.config import ConfigError, load_config


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "commands.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_load_minimal_config(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            server:
              public_base_url: https://example.com
            commands:
              - name: hello
                description: Say hello.
                command: ["echo", "hi"]
            """,
        )
    )
    assert cfg.server.public_base_url == "https://example.com"
    assert [c.name for c in cfg.commands] == ["hello"]
    assert cfg.commands[0].command == ["echo", "hi"]
    assert cfg.commands[0].params == []
    assert cfg.server.tls.enabled is False
    assert cfg.server.transport == "http"


def test_load_stdio_server_transport(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            server:
              transport: stdio
            commands:
              - name: hello
                description: Say hello.
                command: ["echo", "hi"]
            """,
        )
    )
    assert cfg.server.transport == "stdio"


def test_load_tls_config(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            server:
              public_base_url: https://example.com
              tls:
                enabled: true
                auto_generate: true
                hostnames: [internal.example.com, 10.0.0.5]
            commands:
              - name: hello
                description: Say hello.
                command: ["echo", "hi"]
            """,
        )
    )
    assert cfg.server.tls.enabled is True
    assert cfg.server.tls.auto_generate is True
    assert cfg.server.tls.hostnames == ["internal.example.com", "10.0.0.5"]


def test_full_command_with_params_and_defaults(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            server: {public_base_url: http://x}
            defaults:
              timeout: 5
              cwd: /tmp
            commands:
              - name: tail_log
                description: Tail a log.
                command: ["tail", "-n", "{lines}", "/var/log/{file}"]
                params:
                  lines: { type: integer, default: 100 }
                  file:
                    type: string
                    pattern: '^[A-Za-z0-9._-]+$'
                timeout: 10
            """,
        )
    )
    assert cfg.defaults.timeout == 5.0
    assert cfg.defaults.cwd == "/tmp"
    cmd = cfg.commands[0]
    assert cmd.timeout == 10.0
    assert {p.name for p in cmd.params} == {"lines", "file"}
    lines = next(p for p in cmd.params if p.name == "lines")
    assert lines.has_default is True and lines.default == 100
    file_p = next(p for p in cmd.params if p.name == "file")
    assert file_p.pattern == "^[A-Za-z0-9._-]+$"
    assert file_p.has_default is False


@pytest.mark.parametrize(
    "body, fragment",
    [
        # missing commands
        ("server: {public_base_url: x}\n", "must be a non-empty list"),
        # string command form
        (
            """
            server: {public_base_url: x}
            commands:
              - name: bad
                description: x
                command: "echo hi"
            """,
            "must be a non-empty list",
        ),
        # invalid name
        (
            """
            server: {public_base_url: x}
            commands:
              - name: "1bad"
                description: x
                command: ["a"]
            """,
            "command name must match",
        ),
        # missing description
        (
            """
            server: {public_base_url: x}
            commands:
              - name: a
                command: ["x"]
            """,
            "description is required",
        ),
        # placeholder not declared
        (
            """
            server: {public_base_url: x}
            commands:
              - name: a
                description: x
                command: ["echo", "{missing}"]
            """,
            "has no matching params entry",
        ),
        # declared param unused
        (
            """
            server: {public_base_url: x}
            commands:
              - name: a
                description: x
                command: ["echo"]
                params:
                  unused: { type: string }
            """,
            "declared params not used",
        ),
        # duplicate name
        (
            """
            server: {public_base_url: x}
            commands:
              - name: dup
                description: a
                command: ["a"]
              - name: dup
                description: b
                command: ["b"]
            """,
            "duplicate command name",
        ),
        # invalid param type
        (
            """
            server: {public_base_url: x}
            commands:
              - name: a
                description: x
                command: ["echo", "{p}"]
                params:
                  p: { type: blob }
            """,
            "params.p.type must be one of",
        ),
        # invalid regex
        (
            """
            server: {public_base_url: x}
            commands:
              - name: a
                description: x
                command: ["echo", "{p}"]
                params:
                  p: { type: string, pattern: "[" }
            """,
            "not a valid regex",
        ),
        # non-string in argv list
        (
            """
            server: {public_base_url: x}
            commands:
              - name: a
                description: x
                command: ["echo", 1]
            """,
            "must be a string",
        ),
        # malformed TLS configuration
        (
            """
            server:
              public_base_url: https://x
              tls: {enabled: true}
            commands:
              - name: a
                description: x
                command: ["a"]
            """,
            "requires cert_file/key_file or auto_generate",
        ),
        (
            """
            server:
              public_base_url: https://x
              tls:
                enabled: true
                cert_file: cert.pem
            commands:
              - name: a
                description: x
                command: ["a"]
            """,
            "must be provided together",
        ),
        (
            """
            server: {transport: websocket}
            commands:
              - name: a
                description: x
                command: ["a"]
            """,
            "server.transport must be one of",
        ),
    ],
)
def test_invalid_configs_rejected(tmp_path: Path, body: str, fragment: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, body))
    assert fragment in str(exc.value)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nope.yaml")
    assert "not found" in str(exc.value)


def test_load_proxy_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMOTE_MCP_AUTH", "Bearer env-token")
    cfg = load_config(
        write(
            tmp_path,
            """
            mode: proxy
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
                headers:
                  Authorization: {env: REMOTE_MCP_AUTH}
                  X-Project: {value: compile}
            """,
        )
    )

    assert cfg.mode == "proxy"
    assert cfg.commands == []
    assert cfg.proxy is not None
    compile_target, http_target = cfg.proxy.targets
    assert compile_target.ssh is not None
    assert compile_target.ssh.host == "compile-machine"
    assert compile_target.sync is not None
    assert compile_target.sync.excludes == [".git/**", "**/*.pyc"]
    assert compile_target.sync.delete is False
    assert compile_target.sync.enabled is True
    assert http_target.headers["Authorization"].env == "REMOTE_MCP_AUTH"
    assert http_target.headers["X-Project"].value == "compile"


def test_load_remote_config_proxy_target(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            mode: proxy
            proxy:
              compile:
                ssh:
                  host: compile-machine
                config: /etc/rcm/commands.yaml
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
    assert target.sync is not None
    assert target.sync.enabled is False


@pytest.mark.parametrize(
    "body, fragment",
    [
        (
            """
            mode: proxy
            proxy:
              target:
                ssh: {host: compile-machine}
                config: relative/commands.yaml
            """,
            "absolute",
        ),
        (
            """
            mode: proxy
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
            mode: proxy
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
            mode: proxy
            proxy:
              target:
                config: /etc/rcm/commands.yaml
            """,
            "ssh is required",
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
            mode: proxy
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
            mode: proxy
            commands: []
            """,
            "`proxy` is required",
        ),
        (
            """
            mode: proxy
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
            mode: proxy
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
            mode: proxy
            commands: []
            proxy:
              target:
                transport: http
            """,
            "endpoint is required",
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
