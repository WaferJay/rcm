"""Tests for direct command-line tool invocation."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

import rcm.cli as cli
from rcm.artifacts import RCM_RESULT_SCHEMA
from rcm.cli import (
    CLIUsageError,
    merge_tool_arguments,
    parse_arguments_json,
    parse_named_arguments,
    tool_result_envelope,
    tool_result_exit_code,
)
from rcm.sync import SyncError


def _write_config(path: Path, value: dict) -> Path:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def _local_config(path: Path) -> Path:
    return _write_config(
        path,
        {
            "commands": [
                {
                    "name": "echo_values",
                    "description": "Echo typed values.",
                    "command": [
                        sys.executable,
                        "-c",
                        "import sys; print(sys.argv[1], sys.argv[2])",
                        "{word}",
                        "{count}",
                    ],
                    "params": {
                        "word": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                },
                {
                    "name": "fail_seven",
                    "description": "Exit with status seven.",
                    "command": [sys.executable, "-c", "raise SystemExit(7)"],
                },
            ]
        },
    )


def _proxy_config(
    path: Path,
    source: Path,
    remote_code: str,
    *,
    sync_enabled: bool = True,
) -> Path:
    sync: dict[str, object]
    if sync_enabled:
        sync = {
            "source": str(source),
            "destination": str(path.parent / "destination"),
        }
    else:
        sync = {"enabled": False}
    return _write_config(
        path,
        {
            "proxy": {
                "compile": {
                    "transport": "stdio",
                    "command": [sys.executable, "-c", remote_code],
                    "sync": sync,
                }
            }
        },
    )


def test_parse_arguments_json_requires_an_object() -> None:
    assert parse_arguments_json('{"value": 3}') == {"value": 3}
    with pytest.raises(CLIUsageError, match="JSON object"):
        parse_arguments_json("[1, 2]")
    with pytest.raises(CLIUsageError, match="not valid JSON"):
        parse_arguments_json("{")
    with pytest.raises(CLIUsageError, match="duplicate key"):
        parse_arguments_json('{"value": 1, "value": 2}')


def test_parse_named_arguments_decodes_json_and_falls_back_to_strings() -> None:
    assert parse_named_arguments(
        ["count=3", "enabled=true", "name=main", 'payload={"key": 1}']
    ) == {
        "count": 3,
        "enabled": True,
        "name": "main",
        "payload": {"key": 1},
    }


@pytest.mark.parametrize("value", ["missing-separator", "=value", " name=value"])
def test_parse_named_arguments_rejects_invalid_syntax(value: str) -> None:
    with pytest.raises(CLIUsageError):
        parse_named_arguments([value])


def test_merge_tool_arguments_rejects_every_duplicate_source() -> None:
    with pytest.raises(CLIUsageError, match="duplicate --arg"):
        merge_tool_arguments(None, ["value=1", "value=2"])
    with pytest.raises(CLIUsageError, match="both --args and --arg"):
        merge_tool_arguments('{"value": 1}', ["value=2"])


def test_tool_result_envelope_and_exit_status() -> None:
    result = ToolResult(
        content=[TextContent(type="text", text="完成")],
        structured_content={
            "schema": RCM_RESULT_SCHEMA,
            "returncode": 7,
        },
        meta={"source": "test"},
    )

    assert tool_result_envelope(result) == {
        "content": [
            {
                "type": "text",
                "text": "完成",
                "annotations": None,
                "_meta": None,
            }
        ],
        "structured_content": {
            "schema": RCM_RESULT_SCHEMA,
            "returncode": 7,
        },
        "meta": {"source": "test"},
        "is_error": False,
    }
    assert tool_result_exit_code(result) == 7
    assert tool_result_exit_code(
        ToolResult(content="error", structured_content={}, is_error=True)
    ) == 1
    assert tool_result_exit_code(
        ToolResult(
            content="signal",
            structured_content={
                "schema": RCM_RESULT_SCHEMA,
                "returncode": -9,
            },
        )
    ) == 1
    assert tool_result_exit_code(
        ToolResult(
            content="invalid",
            structured_content={
                "schema": RCM_RESULT_SCHEMA,
                "returncode": False,
            },
        )
    ) == 1
    assert tool_result_exit_code(ToolResult(content="ordinary")) == 0


def test_legacy_server_invocation_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "server_main", lambda argv: calls.append(argv))

    assert cli.main([]) == 0
    assert cli.main(["--stdio"]) == 0
    assert calls == [[], ["--stdio"]]


def test_list_outputs_sorted_tool_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _local_config(tmp_path / "commands.yaml")
    monkeypatch.setenv("RCM_RUNS_DIR", str(tmp_path / "runs"))
    stdout = io.StringIO()
    stderr = io.StringIO()

    status = cli.main(
        ["list", "--config", str(config)],
        stdout=stdout,
        stderr=stderr,
    )

    assert status == 0
    assert stderr.getvalue() == ""
    data = json.loads(stdout.getvalue())
    assert [tool["name"] for tool in data] == ["echo_values", "fail_seven"]
    assert data[0]["description"].startswith("Echo typed values.")
    assert set(data[0]["input_schema"]["properties"]) == {"word", "count"}
    assert data[0]["output_schema"]["type"] == "object"


def test_explicit_config_takes_precedence_over_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _local_config(tmp_path / "commands.yaml")
    monkeypatch.setenv("RCM_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("RCM_RUNS_DIR", str(tmp_path / "runs"))
    stdout = io.StringIO()

    status = cli.main(
        ["list", "--config", str(config)],
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert status == 0
    assert len(json.loads(stdout.getvalue())) == 2


def test_call_merges_json_and_named_arguments_and_uses_local_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _local_config(tmp_path / "commands.yaml")
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("RCM_RUNS_DIR", str(runs_dir))
    stdout = io.StringIO()

    status = cli.main(
        [
            "call",
            "--config",
            str(config),
            "echo_values",
            "--args",
            '{"word": "你好"}',
            "--arg",
            "count=2",
        ],
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert status == 0
    envelope = json.loads(stdout.getvalue())
    assert set(envelope) == {"content", "structured_content", "meta", "is_error"}
    result = envelope["structured_content"]
    assert result["schema"] == RCM_RESULT_SCHEMA
    assert result["returncode"] == 0
    stdout_path = Path(result["stdout"]["uri"].removeprefix("file://"))
    assert stdout_path.read_text(encoding="utf-8") == "你好 2\n"


def test_call_propagates_configured_command_returncode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _local_config(tmp_path / "commands.yaml")
    monkeypatch.setenv("RCM_RUNS_DIR", str(tmp_path / "runs"))
    stdout = io.StringIO()

    status = cli.main(
        ["call", "--config", str(config), "fail_seven"],
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert status == 7
    assert json.loads(stdout.getvalue())["structured_content"]["returncode"] == 7


def test_proxy_call_runs_configured_sync_before_remote_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_code = """
from fastmcp import FastMCP

mcp = FastMCP('remote')

@mcp.tool
def echo(value: str) -> dict:
    return {'value': value}

mcp.run()
    """
    source = tmp_path / "source"
    source.mkdir()
    config = _proxy_config(
        tmp_path / "commands.yaml",
        source,
        remote_code,
    )
    monkeypatch.setenv("RCM_RUNS_DIR", str(tmp_path / "runs"))
    sync_calls: list[str] = []

    async def fake_sync(sync_runner) -> None:
        sync_calls.append(sync_runner.target.name)

    monkeypatch.setattr("rcm.sync.SyncRunner.sync", fake_sync)
    stdout = io.StringIO()

    status = cli.main(
        [
            "call",
            "--config",
            str(config),
            "compile__echo",
            "--arg",
            "value=ready",
        ],
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert status == 0
    assert sync_calls == ["compile"]
    envelope = json.loads(stdout.getvalue())
    assert envelope["structured_content"] == {"value": "ready"}


def test_sync_failure_prevents_remote_tool_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "remote-called"
    remote_code = f"""
from pathlib import Path
from fastmcp import FastMCP

mcp = FastMCP('remote')

@mcp.tool
def touch() -> str:
    Path({str(marker)!r}).write_text('called')
    return 'called'

mcp.run()
"""
    source = tmp_path / "source"
    source.mkdir()
    config = _proxy_config(
        tmp_path / "commands.yaml",
        source,
        remote_code,
    )
    monkeypatch.setenv("RCM_RUNS_DIR", str(tmp_path / "runs"))

    async def fail_sync(sync_runner) -> None:
        raise SyncError(f"sync failed for {sync_runner.target.name}")

    monkeypatch.setattr("rcm.sync.SyncRunner.sync", fail_sync)
    stdout = io.StringIO()
    stderr = io.StringIO()

    status = cli.main(
        ["call", "--config", str(config), "compile__touch"],
        stdout=stdout,
        stderr=stderr,
    )

    assert status == 1
    assert stdout.getvalue() == ""
    assert "sync failed for compile" in stderr.getvalue()
    assert not marker.exists()


def test_disabled_sync_is_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_code = """
from fastmcp import FastMCP

mcp = FastMCP('remote')

@mcp.tool
def ready() -> str:
    return 'ready'

mcp.run()
"""
    source = tmp_path / "source"
    source.mkdir()
    config = _proxy_config(
        tmp_path / "commands.yaml",
        source,
        remote_code,
        sync_enabled=False,
    )
    monkeypatch.setenv("RCM_RUNS_DIR", str(tmp_path / "runs"))

    async def unexpected_sync(sync_runner) -> None:
        raise AssertionError(f"unexpected sync for {sync_runner.target.name}")

    monkeypatch.setattr("rcm.sync.SyncRunner.sync", unexpected_sync)
    stdout = io.StringIO()

    status = cli.main(
        ["call", "--config", str(config), "compile__ready"],
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert status == 0
    assert json.loads(stdout.getvalue())["is_error"] is False


def test_application_usage_errors_exit_with_status_two(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["call", "tool", "--args", "[]"])

    assert exc_info.value.code == 2
    assert "--args must be a JSON object" in capsys.readouterr().err


def test_runtime_errors_use_stderr_and_status_one(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    status = cli.main(
        ["list", "--config", str(tmp_path / "missing.yaml")],
        stdout=stdout,
        stderr=stderr,
    )

    assert status == 1
    assert stdout.getvalue() == ""
    assert "rcm: list failed:" in stderr.getvalue()
    assert "config file not found" in stderr.getvalue()
