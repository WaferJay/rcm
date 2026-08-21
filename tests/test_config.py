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


def test_load_webdav_config(tmp_path: Path) -> None:
    cfg = load_config(
        write(
            tmp_path,
            """
            server: {public_base_url: http://x}
            webdav:
              root: /srv/files
              path: files
              read_only: false
              hide:
                rcm: false
                config: false
                glob: ["**/*.tmp", "secret/**"]
              auth:
                type: basic
                username: alice
                password: secret
            commands:
              - name: hello
                description: Say hello.
                command: ["echo", "hi"]
            """,
        )
    )
    assert cfg.webdav is not None
    assert cfg.webdav.root == "/srv/files"
    assert cfg.webdav.path == "/files/"
    assert cfg.webdav.read_only is False
    assert cfg.webdav.auth.type == "basic"
    assert cfg.webdav.auth.username == "alice"
    assert cfg.webdav.hide.rcm is False
    assert cfg.webdav.hide.config is False
    assert cfg.webdav.hide.glob == ["**/*.tmp", "secret/**"]
    assert cfg.config_path == (tmp_path / "commands.yaml").resolve()


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
    assert http_target.headers["Authorization"].env == "REMOTE_MCP_AUTH"
    assert http_target.headers["X-Project"].value == "compile"


@pytest.mark.parametrize(
    "webdav, fragment",
    [
        ("null", "must be a mapping"),
        ("{root: /tmp, path: /}", "must not be `/`"),
        ("{root: /tmp, path: /mcp/files}", "conflicts with a reserved path"),
        ("{root: /tmp, read_only: 1}", "must be a boolean"),
        ("{root: /tmp, hide: {rcm: 1}}", "webdav.hide.rcm must be a boolean"),
        ("{root: /tmp, hide: {config: 1}}", "webdav.hide.config must be a boolean"),
        ("{root: /tmp, hide: {glob: '*.tmp'}}", "webdav.hide.glob must be a list"),
        (
            "{root: /tmp, hide: {glob: ['../secret']}}",
            "must not contain empty, `.` or `..` path parts",
        ),
        (
            "{root: /tmp, hide: {glob: ['[abc']}}",
            "has an invalid character class",
        ),
        ("{root: /tmp, auth: {type: unknown}}", "must be one of"),
        ("{root: /tmp, auth: {type: basic}}", "are required for basic auth"),
    ],
)
def test_invalid_webdav_configs_rejected(
    tmp_path: Path, webdav: str, fragment: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(
            write(
                tmp_path,
                f"""
                server: {{public_base_url: http://x}}
                webdav: {webdav}
                commands:
                  - name: hello
                    description: Say hello.
                    command: ["echo", "hi"]
                """,
            )
        )
    assert fragment in str(exc.value)


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
