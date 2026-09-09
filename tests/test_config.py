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


def test_load_command_collect_paths(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            commands:
              - name: build
                description: Build artifacts.
                command: [make, build]
                collect:
                  on_exit: always
                  mode: changed
                  paths:
                    - path: dist/**/*.tar.gz
                      required: true
                    - path: reports/*
                      on_exit: success
                      mode: always
            """,
        )
    )

    collect = cfg.commands[0].collect
    assert collect is not None
    assert collect.on_exit == "always"
    assert collect.mode == "changed"
    assert [
        (item.path, item.required, item.on_exit, item.mode)
        for item in collect.paths
    ] == [
        ("dist/**/*.tar.gz", True, None, None),
        ("reports/*", False, "success", "always"),
    ]


def test_command_collect_defaults_preserve_successful_full_collection(
    tmp_path: Path,
) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            commands:
              - name: build
                description: Build artifacts.
                command: [make, build]
                collect:
                  paths:
                    - path: dist
            """,
        )
    )

    collect = cfg.commands[0].collect
    assert collect is not None
    assert collect.on_exit == "success"
    assert collect.mode == "always"


@pytest.mark.parametrize(
    "collect_body,fragment",
    [
        ("{}", "non-empty list"),
        ("{paths: []}", "non-empty list"),
        ("{paths: [artifact.txt]}", "must be a mapping"),
        ("{paths: [{path: /absolute}]}", "relative POSIX path"),
        ("{paths: [{path: ../escape}]}", "must not contain"),
        ("{paths: [{path: dir/}]}", "must not contain"),
        ("{paths: [{path: 'dist/**.whl'}]}", "complete path part"),
        ("{paths: [{path: out, required: 1}]}", "must be a boolean"),
        ("{on_exit: failure, paths: [{path: out}]}", "on_exit must be one of"),
        ("{mode: newer, paths: [{path: out}]}", "mode must be one of"),
        ("{paths: [{path: out, on_exit: failure}]}", "on_exit must be one of"),
        ("{paths: [{path: out, mode: newer}]}", "mode must be one of"),
    ],
)
def test_invalid_command_collect_rejected(
    tmp_path: Path, collect_body: str, fragment: str
) -> None:
    with pytest.raises(ConfigError, match=fragment):
        load_config(
            write(
                tmp_path,
                f"""
                commands:
                  - name: build
                    description: Build artifacts.
                    command: [make, build]
                    collect: {collect_body}
                """,
            )
        )


@pytest.mark.parametrize(
    "body, fragment",
    [
        # missing commands and proxy
        ("server: {public_base_url: x}\n", "at least one of `commands` or `proxy`"),
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
    assert target.sync is not None
    assert target.sync.enabled is False
    assert target.artifacts == "passthrough"


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
