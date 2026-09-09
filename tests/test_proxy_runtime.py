"""Tests for proxy runtime orchestration and integration."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastmcp import FastMCP

from rcm.config import (
    AuthSpec,
    CollectPathSpec,
    CollectSpec,
    CommandSpec,
    Config,
    DefaultsSpec,
    ProxySpec,
    ProxyTargetSpec,
    ServerSpec,
)
from rcm.proxy import ProxyError
from rcm.server import build_proxy_server, build_server
from rcm.store import Store


@pytest.mark.asyncio
async def test_proxy_runtime_wraps_initial_connection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rcm.proxy.runtime as proxy_module

    class FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            raise RuntimeError("connection refused")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            pass

    monkeypatch.setattr(proxy_module, "Client", FailingClient)
    cfg = Config(
        server=ServerSpec(),
        auth=AuthSpec(),
        defaults=DefaultsSpec(),
        commands=[],
        proxy=ProxySpec(
            targets=[
                ProxyTargetSpec(
                    name="remote",
                    transport="stdio",
                    command=["unused"],
                )
            ]
        ),
    )

    with pytest.raises(ProxyError, match="failed to connect: connection refused"):
        await build_proxy_server(
            cfg,
            Store(tmp_path / "runs", (tmp_path / "runs").as_uri()),
            None,
        )


@pytest.mark.asyncio
async def test_proxy_runtime_discovers_and_registers_stdio_tools(tmp_path) -> None:
    import sys

    remote_code = """
from fastmcp import FastMCP

mcp = FastMCP('remote')

@mcp.tool
def remote_echo(value: str) -> str:
    return value

mcp.run()
"""
    cfg = Config(
        server=ServerSpec(public_base_url="http://testserver"),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[
            CommandSpec(
                name="local_echo",
                description="Echo locally.",
                command=[sys.executable, "-c", "print('local')"],
            )
        ],
        proxy=ProxySpec(
            targets=[
                ProxyTargetSpec(
                    name="compile",
                    transport="stdio",
                    command=[sys.executable, "-c", remote_code],
                )
            ]
        ),
    )
    mcp, runtime = await build_proxy_server(
        cfg,
        Store(tmp_path / "runs", "http://testserver"),
        "key",
    )
    try:
        tools = await mcp._list_tools()
        assert {tool.name for tool in tools} == {"local_echo", "compile__remote_echo"}
        tool = await mcp.get_tool("compile__remote_echo")
        assert tool is not None
        result = await tool.run({"value": "ok"})
        assert result.content[0].text == "ok"
        local_tool = await mcp.get_tool("local_echo")
        assert local_tool is not None
        local_result = await local_tool.run({})
        assert local_result.structured_content["stdout"]["bytes"] > 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_explicit_artifact_mode_rejects_non_rcm_target(tmp_path) -> None:
    import sys

    remote_code = """
from fastmcp import FastMCP
mcp = FastMCP('ordinary-mcp')
@mcp.tool
def echo(value: str) -> str:
    return value
mcp.run()
"""
    cfg = Config(
        server=ServerSpec(),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[],
        proxy=ProxySpec(
            targets=[
                ProxyTargetSpec(
                    name="ordinary",
                    transport="stdio",
                    command=[sys.executable, "-c", remote_code],
                    artifacts="localize",
                )
            ]
        ),
    )

    with pytest.raises(ProxyError, match="target is not an RCM v2 server"):
        await build_proxy_server(
            cfg,
            Store(tmp_path / "runs", (tmp_path / "runs").as_uri()),
            None,
        )


@pytest.mark.asyncio
async def test_proxy_runtime_recovers_remote_rcm_artifact(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    remote_config = tmp_path / "remote-commands.yaml"
    code = (
        "import sys; sys.stdout.buffer.write(bytes([0, 255])); "
        "sys.stderr.buffer.write(b'err')"
    )
    remote_config.write_text(
        f"commands:\n"
        f"  - name: remote_binary\n"
        f"    description: Remote binary output.\n"
        f"    command: [{sys.executable!r}, '-c', {code!r}]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RCM_CONFIG", str(remote_config))
    monkeypatch.setenv("RCM_RUNS_DIR", str(tmp_path / "remote-runs"))

    cfg = Config(
        server=ServerSpec(),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[],
        proxy=ProxySpec(
            targets=[
                ProxyTargetSpec(
                    name="remote",
                    transport="stdio",
                    command=[sys.executable, "-m", "rcm", "--stdio"],
                )
            ]
        ),
    )
    store = Store(tmp_path / "local-runs", (tmp_path / "local-runs").as_uri(), local_urls=True)
    mcp, runtime = await build_proxy_server(cfg, store, None)
    try:
        tool = await mcp.get_tool("remote__remote_binary")
        assert tool is not None
        result = await tool.run({})
        data = result.structured_content
        assert data["stdout"]["uri"].startswith("file://")
        assert data["stderr"]["uri"].startswith("file://")
        assert Path(urlparse(data["stdout"]["uri"]).path).read_bytes() == bytes([0, 255])
        assert Path(urlparse(data["stderr"]["uri"]).path).read_bytes() == b"err"
        assert "stdout_base64" not in data
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_proxy_runtime_bridges_streamable_http(tmp_path) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    remote = FastMCP("remote-http")

    @remote.tool
    def remote_echo(value: str) -> str:
        return value

    remote_task = asyncio.create_task(
        remote.run_async(
            transport="http",
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("remote HTTP server failed to start")
            await asyncio.sleep(0.05)

    cfg = Config(
        server=ServerSpec(public_base_url="http://testserver"),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[],
        proxy=ProxySpec(
            targets=[
                ProxyTargetSpec(
                    name="reports",
                    transport="http",
                    endpoint=f"http://127.0.0.1:{port}/mcp",
                )
            ]
        ),
    )
    runtime = None
    try:
        _, runtime = await build_proxy_server(
            cfg,
            Store(tmp_path / "runs", "http://testserver"),
            "key",
        )
        tool = await runtime.server.get_tool("reports__remote_echo")
        assert tool is not None
        result = await tool.run({"value": "ok"})
        assert result.content[0].text == "ok"
    finally:
        if runtime is not None:
            await runtime.close()
        remote_task.cancel()
        try:
            await remote_task
        except BaseException:
            pass


@pytest.mark.asyncio
async def test_http_rcm_target_localizes_or_passthroughs_artifacts(tmp_path) -> None:
    import sys

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base_url = f"http://127.0.0.1:{port}"
    remote_work = tmp_path / "remote-work"
    remote_work.mkdir()
    remote_cfg = Config(
        server=ServerSpec(public_base_url=base_url),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[
            CommandSpec(
                name="binary",
                description="Return binary output.",
                command=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path;import sys;Path('artifact.bin').write_bytes(b'artifact');sys.stdout.buffer.write(b'out\\x00');sys.stderr.buffer.write(b'err')",
                ],
                cwd=str(remote_work),
                collect=CollectSpec(paths=(CollectPathSpec("artifact.bin"),)),
            )
        ],
    )
    remote_store = Store(tmp_path / "remote-runs", base_url)
    remote = build_server(remote_cfg, remote_store, api_key=None)
    remote_task = asyncio.create_task(
        remote.run_async(
            transport="http",
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("remote HTTP RCM failed to start")
            await asyncio.sleep(0.05)

    async def call_through(mode: str | None, runs_name: str):
        target = ProxyTargetSpec(
            name="remote",
            transport="http",
            endpoint=f"{base_url}/mcp",
            artifacts=mode,
        )
        cfg = Config(
            server=ServerSpec(),
            auth=AuthSpec(api_key=None),
            defaults=DefaultsSpec(),
            commands=[],
            proxy=ProxySpec(targets=[target]),
        )
        local_store = Store(
            tmp_path / runs_name,
            (tmp_path / runs_name).as_uri(),
            local_urls=True,
        )
        mcp, runtime = await build_proxy_server(cfg, local_store, None)
        try:
            tool = await mcp.get_tool("remote__binary")
            assert tool is not None
            result = await tool.run({})
            return result.structured_content, local_store
        finally:
            await runtime.close()

    try:
        localized, local_store = await call_through(None, "localized-runs")
        assert localized["run_id"] not in {
            child.name for child in remote_store.runs_dir.iterdir()
        }
        assert localized["stdout"]["uri"].startswith("file://")
        assert Path(urlparse(localized["stdout"]["uri"]).path).read_bytes() == b"out\x00"
        assert Path(urlparse(localized["stderr"]["uri"]).path).read_bytes() == b"err"
        localized_collect = Path(urlparse(localized["collect"]["uri"]).path)
        assert localized_collect.name == "collect.tar.gz"
        assert localized_collect.read_bytes().startswith(b"\x1f\x8b")
        assert len(list(local_store.runs_dir.iterdir())) == 1

        passed, passthrough_store = await call_through(
            "passthrough", "passthrough-runs"
        )
        assert passed["stdout"]["uri"].startswith(f"{base_url}/runs/")
        assert passed["collect"]["uri"].startswith(f"{base_url}/runs/")
        assert passed["run_id"] in {
            child.name for child in remote_store.runs_dir.iterdir()
        }
        assert list(passthrough_store.runs_dir.iterdir()) == []
    finally:
        remote_task.cancel()
        try:
            await remote_task
        except BaseException:
            pass

