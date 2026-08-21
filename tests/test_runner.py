"""Tests for rcm.runner: param validation, substitution, execution."""

from __future__ import annotations

import json
import base64
import sys
from pathlib import Path

import pytest

from rcm.config import CommandSpec, ParamSpec
from rcm.runner import RunError, _resolve_params, _substitute, run_command
from rcm.store import Store


def make_spec(
    name: str,
    command: list[str],
    params: list[ParamSpec] | None = None,
    *,
    timeout: float | None = None,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        description=f"{name} desc",
        command=command,
        params=params or [],
        timeout=timeout,
        cwd=None,
    )


def make_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "runs", public_base_url="https://example.test")


# ---------- pure helpers ----------

def test_substitute_whole_element_keeps_string() -> None:
    assert _substitute(["{x}", "literal"], {"x": "hello world"}) == [
        "hello world",
        "literal",
    ]


def test_substitute_inline_uses_format_map() -> None:
    out = _substitute(["/var/log/{file}"], {"file": "nginx.log"})
    assert out == ["/var/log/nginx.log"]


def test_substitute_no_placeholder_returns_unchanged() -> None:
    assert _substitute(["just text", "abc"], {}) == ["just text", "abc"]


def test_resolve_params_uses_default() -> None:
    spec = make_spec(
        "x",
        ["echo", "{n}"],
        [ParamSpec(name="n", type="integer", default=7, has_default=True)],
    )
    out = _resolve_params(spec, {})
    assert out == {"n": "7"}


def test_resolve_params_missing_required() -> None:
    spec = make_spec(
        "x", ["echo", "{n}"], [ParamSpec(name="n", type="integer")]
    )
    with pytest.raises(RunError, match="missing required param"):
        _resolve_params(spec, {})


def test_resolve_params_unexpected_extra() -> None:
    spec = make_spec("x", ["echo"], [])
    with pytest.raises(RunError, match="unexpected params"):
        _resolve_params(spec, {"x": 1})


def test_resolve_params_type_check() -> None:
    spec = make_spec(
        "x", ["echo", "{n}"], [ParamSpec(name="n", type="integer")]
    )
    with pytest.raises(RunError, match="must be an integer"):
        _resolve_params(spec, {"n": "not-an-int"})


def test_resolve_params_pattern_enforced() -> None:
    spec = make_spec(
        "x",
        ["echo", "{f}"],
        [ParamSpec(name="f", type="string", pattern=r"^[a-z]+$")],
    )
    with pytest.raises(RunError, match="does not match pattern"):
        _resolve_params(spec, {"f": "../etc"})


def test_resolve_params_enum_enforced() -> None:
    spec = make_spec(
        "x",
        ["echo", "{m}"],
        [ParamSpec(name="m", type="string", enum=["a", "b"])],
    )
    with pytest.raises(RunError, match="not in enum"):
        _resolve_params(spec, {"m": "c"})


def test_resolve_params_boolean_stringified() -> None:
    spec = make_spec(
        "x", ["echo", "{flag}"], [ParamSpec(name="flag", type="boolean")]
    )
    assert _resolve_params(spec, {"flag": True}) == {"flag": "true"}
    assert _resolve_params(spec, {"flag": False}) == {"flag": "false"}


def test_resolve_params_rejects_bool_for_int() -> None:
    spec = make_spec(
        "x", ["echo", "{n}"], [ParamSpec(name="n", type="integer")]
    )
    with pytest.raises(RunError, match="must be an integer"):
        _resolve_params(spec, {"n": True})


# ---------- end-to-end execution ----------

async def test_run_command_success_writes_files(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    spec = make_spec(
        "echo_it",
        [sys.executable, "-c", "import sys; sys.stdout.write('hi'); sys.stderr.write('err')"],
    )
    result = await run_command(
        spec, {}, store=store, default_timeout=10, default_cwd=None
    )
    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert result["stdout_bytes"] == 2
    assert result["stderr_bytes"] == 3
    assert result["stdout_url"].startswith("https://example.test/runs/")
    assert result["stdout_url"].endswith("/stdout")

    rid = result["run_id"]
    assert (store.run_dir(rid) / "stdout.log").read_text() == "hi"
    assert (store.run_dir(rid) / "stderr.log").read_text() == "err"
    meta = json.loads((store.run_dir(rid) / "meta.json").read_text())
    assert meta["command_name"] == "echo_it"
    assert meta["returncode"] == 0


async def test_run_command_inline_artifact_mode_returns_base64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RCM_MCP_PROXY_ARTIFACTS", "inline-base64")
    store = make_store(tmp_path)
    spec = make_spec(
        "binary",
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'\\x00\\xff'); sys.stderr.buffer.write(b'err')",
        ],
    )
    result = await run_command(
        spec, {}, store=store, default_timeout=10, default_cwd=None
    )
    assert result["artifact_protocol"] == "rcm-inline-base64-v1"
    assert base64.b64decode(result["stdout_base64"]) == b"\x00\xff"
    assert base64.b64decode(result["stderr_base64"]) == b"err"
    assert "stdout_url" not in result
    assert "stderr_url" not in result


async def test_run_command_substitutes_params(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    spec = make_spec(
        "say",
        [sys.executable, "-c", "import sys; print('hello', sys.argv[1])", "{who}"],
        [ParamSpec(name="who", type="string", pattern=r"^[a-zA-Z]+$")],
    )
    result = await run_command(
        spec, {"who": "world"}, store=store, default_timeout=10, default_cwd=None
    )
    rid = result["run_id"]
    assert (store.run_dir(rid) / "stdout.log").read_text().strip() == "hello world"


async def test_run_command_timeout(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    spec = make_spec(
        "slow",
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=0.5,
    )
    result = await run_command(
        spec, {}, store=store, default_timeout=None, default_cwd=None
    )
    assert result["timed_out"] is True
    assert result["returncode"] != 0
    rid = result["run_id"]
    assert "timed out" in (store.run_dir(rid) / "stderr.log").read_text()


async def test_run_command_missing_executable(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    spec = make_spec("nope", ["/no/such/binary/please"])
    result = await run_command(
        spec, {}, store=store, default_timeout=5, default_cwd=None
    )
    assert result["returncode"] == 127
    rid = result["run_id"]
    err = (store.run_dir(rid) / "stderr.log").read_text()
    assert "executable not found" in err
    assert result["stderr_bytes"] == len(err.encode())


async def test_run_command_pattern_rejection_does_not_create_run(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    spec = make_spec(
        "tail",
        ["echo", "/var/log/{f}"],
        [ParamSpec(name="f", type="string", pattern=r"^[a-z]+$")],
    )
    with pytest.raises(RunError):
        await run_command(
            spec, {"f": "../etc"}, store=store, default_timeout=5, default_cwd=None
        )
    # Validation happens before the run dir is created.
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").iterdir())
