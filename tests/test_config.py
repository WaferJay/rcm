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
