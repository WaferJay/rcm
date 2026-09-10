"""Command-line entry point for serving and directly invoking RCM tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn, TextIO

from fastmcp.tools.base import Tool, ToolResult

from .artifacts import RCM_RESULT_SCHEMA
from .cli_runtime import ConfiguredToolRuntime
from .server import main as server_main


class CLIUsageError(ValueError):
    """Raised when a CLI argument has invalid application-level syntax."""


def build_parser() -> argparse.ArgumentParser:
    """Build the public command-line grammar."""
    parser = argparse.ArgumentParser(
        prog="rcm",
        description="Run the RCM MCP server or invoke configured tools directly.",
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="serve MCP over stdin/stdout instead of Streamable HTTP",
    )
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser(
        "list",
        help="list tools from the local RCM configuration",
    )
    _add_config_argument(list_parser)

    call_parser = subparsers.add_parser(
        "call",
        help="call a tool from the local RCM configuration",
    )
    _add_config_argument(call_parser)
    call_parser.add_argument("tool", help="configured tool name")
    call_parser.add_argument(
        "--args",
        dest="arguments_json",
        metavar="JSON",
        help="tool arguments as a JSON object",
    )
    call_parser.add_argument(
        "--arg",
        dest="named_arguments",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "one tool argument; VALUE is decoded as JSON when possible "
            "and otherwise kept as a string (repeatable)"
        ),
    )
    return parser


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="RCM config path (default: RCM_CONFIG or commands.yaml)",
    )


def _config_path(explicit: str | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get("RCM_CONFIG", "commands.yaml"))


def _reject_duplicate_json_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CLIUsageError(f"duplicate key in --args JSON: {key!r}")
        value[key] = item
    return value


def parse_arguments_json(raw: str | None) -> dict[str, Any]:
    """Decode the optional ``--args`` object without silent duplicate keys."""
    if raw is None:
        return {}
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_key)
    except CLIUsageError:
        raise
    except json.JSONDecodeError as exc:
        raise CLIUsageError(f"--args is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise CLIUsageError("--args must be a JSON object")
    return value


def parse_named_arguments(values: Sequence[str]) -> dict[str, Any]:
    """Decode repeatable ``--arg NAME=VALUE`` inputs."""
    arguments: dict[str, Any] = {}
    for raw in values:
        if "=" not in raw:
            raise CLIUsageError(f"--arg must use NAME=VALUE syntax: {raw!r}")
        name, encoded = raw.split("=", 1)
        if not name or name != name.strip():
            raise CLIUsageError(f"--arg has an invalid name: {name!r}")
        if name in arguments:
            raise CLIUsageError(f"duplicate --arg name: {name!r}")
        try:
            arguments[name] = json.loads(encoded)
        except json.JSONDecodeError:
            arguments[name] = encoded
    return arguments


def merge_tool_arguments(
    arguments_json: str | None,
    named_arguments: Sequence[str],
) -> dict[str, Any]:
    """Merge JSON and named argument sources, rejecting ambiguous overrides."""
    arguments = parse_arguments_json(arguments_json)
    named = parse_named_arguments(named_arguments)
    duplicates = sorted(arguments.keys() & named.keys())
    if duplicates:
        raise CLIUsageError(
            "arguments are present in both --args and --arg: "
            + ", ".join(repr(name) for name in duplicates)
        )
    arguments.update(named)
    return arguments


def tool_descriptor(tool: Tool) -> dict[str, Any]:
    """Convert a FastMCP tool into the stable CLI list representation."""
    descriptor: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }
    if tool.output_schema is not None:
        descriptor["output_schema"] = tool.output_schema
    return descriptor


def tool_result_envelope(result: ToolResult) -> dict[str, Any]:
    """Convert a FastMCP result into the stable CLI call representation."""
    return result.model_dump(mode="json", by_alias=True)


def tool_result_exit_code(result: ToolResult) -> int:
    """Map an MCP result to a process status without losing RCM return codes."""
    if result.is_error:
        return 1
    structured = result.structured_content
    if (
        not isinstance(structured, dict)
        or structured.get("schema") != RCM_RESULT_SCHEMA
    ):
        return 0
    returncode = structured.get("returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        return 1
    if returncode == 0:
        return 0
    if 1 <= returncode <= 255:
        return returncode
    return 1


def _write_json(value: Any, stream: TextIO) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


async def _list_tools(config_path: Path, stdout: TextIO) -> int:
    runtime = await ConfiguredToolRuntime.open(config_path)
    async with runtime:
        tools = await runtime.list_tools()
    descriptors = [tool_descriptor(tool) for tool in tools]
    descriptors.sort(key=lambda item: item["name"])
    _write_json(descriptors, stdout)
    return 0


async def _call_tool(
    config_path: Path,
    tool_name: str,
    arguments: dict[str, Any],
    stdout: TextIO,
) -> int:
    runtime = await ConfiguredToolRuntime.open(config_path)
    async with runtime:
        result = await runtime.call_tool(tool_name, arguments)
    _write_json(tool_result_envelope(result), stdout)
    return tool_result_exit_code(result)


def _usage_error(parser: argparse.ArgumentParser, message: str) -> NoReturn:
    parser.error(message)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the RCM CLI and return its process exit status."""
    output = stdout if stdout is not None else sys.stdout
    errors = stderr if stderr is not None else sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        server_args = ["--stdio"] if args.stdio else []
        server_main(server_args)
        return 0
    if args.stdio:
        _usage_error(parser, "--stdio cannot be combined with list or call")

    try:
        if args.command == "list":
            return asyncio.run(_list_tools(_config_path(args.config), output))
        if args.command == "call":
            arguments = merge_tool_arguments(
                args.arguments_json,
                args.named_arguments,
            )
            return asyncio.run(
                _call_tool(
                    _config_path(args.config),
                    args.tool,
                    arguments,
                    output,
                )
            )
        raise AssertionError(f"unhandled command: {args.command}")
    except CLIUsageError as exc:
        _usage_error(parser, str(exc))
    except KeyboardInterrupt:
        print("rcm: interrupted", file=errors)
        return 130
    except Exception as exc:
        print(f"rcm: {args.command} failed: {exc}", file=errors)
        return 1
