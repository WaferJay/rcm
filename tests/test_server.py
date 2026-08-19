"""Tests for rcm.server: dynamic tool registration and download routes."""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from rcm.config import (
    AuthSpec,
    CommandSpec,
    Config,
    DefaultsSpec,
    ParamSpec,
    ServerSpec,
    WebDAVAuthSpec,
    WebDAVSpec,
)
from rcm.server import _build_tool_fn, build_server
from rcm.store import Store


def make_store(tmp_path: Path) -> Store:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    return Store(runs, public_base_url="https://example.test")


def test_build_tool_fn_signature_matches_params(tmp_path: Path) -> None:
    spec = CommandSpec(
        name="t",
        description="hello",
        command=["echo", "{a}", "{b}"],
        params=[
            ParamSpec(name="a", type="string"),
            ParamSpec(name="b", type="integer", default=7, has_default=True),
        ],
    )
    fn = _build_tool_fn(spec, default_timeout=None, default_cwd=None, store=make_store(tmp_path))
    assert fn.__name__ == "t"
    assert "hello" in (fn.__doc__ or "")
    sig = inspect.signature(fn)
    assert list(sig.parameters) == ["a", "b"]
    assert sig.parameters["a"].annotation is str
    assert sig.parameters["b"].annotation is int
    assert sig.parameters["b"].default == 7
    assert sig.parameters["a"].default is inspect.Parameter.empty


@pytest.fixture
def free_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def running_server(tmp_path: Path, free_port: int):
    """Start a real FastMCP HTTP server in a background task on a free port."""
    cfg = Config(
        server=ServerSpec(host="127.0.0.1", port=free_port, public_base_url=f"http://127.0.0.1:{free_port}"),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(timeout=5, cwd=None),
        commands=[
            CommandSpec(
                name="echo_hi",
                description="Print hi.",
                command=[sys.executable, "-c", "print('hi')"],
                params=[],
            ),
            CommandSpec(
                name="echo_arg",
                description="Echo arg.",
                command=[sys.executable, "-c", "import sys; print(sys.argv[1])", "{w}"],
                params=[ParamSpec(name="w", type="string", pattern=r"^[a-z]+$")],
            ),
        ],
    )
    store = Store(tmp_path / "runs", public_base_url=f"http://127.0.0.1:{free_port}")
    mcp = build_server(cfg, store, api_key="testkey")

    async def _serve():
        await mcp.run_async(transport="http", host="127.0.0.1", port=free_port, log_level="warning")

    task = asyncio.create_task(_serve())
    # Wait until socket is accepting.
    import socket
    deadline = asyncio.get_event_loop().time() + 5
    while True:
        try:
            with socket.create_connection(("127.0.0.1", free_port), timeout=0.2):
                break
        except OSError:
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError("server failed to start")
            await asyncio.sleep(0.05)

    try:
        yield {"port": free_port, "store": store, "url": f"http://127.0.0.1:{free_port}"}
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def make_client(url: str, api_key: str | None) -> Client:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    return Client(StreamableHttpTransport(url=url + "/mcp", headers=headers))


async def test_list_tools_with_valid_key(running_server) -> None:
    client = make_client(running_server["url"], "testkey")
    async with client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == {"echo_hi", "echo_arg"}
    arg_tool = next(t for t in tools if t.name == "echo_arg")
    assert "w" in (arg_tool.inputSchema or {}).get("properties", {})


async def test_list_tools_without_key_rejected(running_server) -> None:
    client = make_client(running_server["url"], None)
    with pytest.raises(Exception):
        async with client:
            await client.list_tools()


async def test_list_tools_wrong_key_rejected(running_server) -> None:
    client = make_client(running_server["url"], "wrong")
    with pytest.raises(Exception):
        async with client:
            await client.list_tools()


async def test_call_tool_returns_capability_urls(running_server) -> None:
    client = make_client(running_server["url"], "testkey")
    async with client:
        r = await client.call_tool("echo_hi", {})
    data = r.data
    assert data["returncode"] == 0
    assert data["timed_out"] is False
    base = running_server["url"]
    assert data["stdout_url"].startswith(f"{base}/runs/")
    assert data["stdout_url"].endswith("/stdout")
    assert data["stderr_url"].endswith("/stderr")


async def test_call_tool_param_validation(running_server) -> None:
    client = make_client(running_server["url"], "testkey")
    async with client:
        # pattern violation -> ToolError
        with pytest.raises(Exception, match="pattern"):
            await client.call_tool("echo_arg", {"w": "Bad-Value"})


async def test_download_endpoints_are_public(running_server) -> None:
    import httpx

    client = make_client(running_server["url"], "testkey")
    async with client:
        r = await client.call_tool("echo_hi", {})
    base = running_server["url"]
    rid = r.data["run_id"]

    async with httpx.AsyncClient() as h:
        # No Authorization header — public capability URL.
        resp = await h.get(f"{base}/runs/{rid}/stdout")
        assert resp.status_code == 200
        assert resp.text.strip() == "hi"

        resp = await h.get(f"{base}/runs/{rid}/stderr")
        assert resp.status_code == 200
        assert resp.text == ""

        resp = await h.get(f"{base}/runs/{rid}/meta")
        assert resp.status_code == 200
        meta = resp.json()
        assert meta["run_id"] == rid
        assert meta["command_name"] == "echo_hi"


async def test_download_tail_returns_suffix(running_server) -> None:
    import httpx

    client = make_client(running_server["url"], "testkey")
    async with client:
        r = await client.call_tool("echo_hi", {})
    base = running_server["url"]
    rid = r.data["run_id"]
    async with httpx.AsyncClient() as h:
        resp = await h.get(f"{base}/runs/{rid}/stdout?tail=1")
        assert resp.status_code == 200
        assert resp.content == b"\n"  # last byte of "hi\n"


async def test_download_invalid_run_id(running_server) -> None:
    import httpx

    base = running_server["url"]
    async with httpx.AsyncClient() as h:
        # Path traversal blocked at the URL level (Starlette won't even route).
        resp = await h.get(f"{base}/runs/not-a-real-id/stdout")
        assert resp.status_code == 404
        # Bad characters in run_id -> 400
        resp = await h.get(f"{base}/runs/has%20space/stdout")
        assert resp.status_code == 400


async def test_healthz(running_server) -> None:
    import httpx

    base = running_server["url"]
    async with httpx.AsyncClient() as h:
        resp = await h.get(f"{base}/healthz")
        assert resp.status_code == 200
        assert resp.text == "ok"


def test_webdav_root_validation(tmp_path: Path) -> None:
    from rcm.webdav import WebDAVError, resolve_webdav_root

    spec = WebDAVSpec(root=str(tmp_path / "missing"))
    with pytest.raises(WebDAVError, match="does not exist"):
        resolve_webdav_root(spec)


@pytest.mark.asyncio
async def test_webdav_readonly_mount(tmp_path: Path) -> None:
    import httpx
    import xml.etree.ElementTree as ET

    from rcm.server import build_http_app

    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    cfg = Config(
        server=ServerSpec(public_base_url="http://testserver"),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[],
        webdav=WebDAVSpec(
            root=str(tmp_path),
            auth=WebDAVAuthSpec(type="none"),
        ),
    )
    # The HTTP app only needs a FastMCP instance for this routing test.
    mcp = build_server(cfg, Store(tmp_path / "runs", "http://testserver"), "key")
    app = build_http_app(mcp, cfg, "key")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as h:
        resp = await h.get("/healthz")
        assert resp.status_code == 200
        assert resp.text == "ok"

        resp = await h.get("/webdav/hello.txt")
        assert resp.status_code == 200
        assert resp.text == "hello"

        resp = await h.request("PROPFIND", "/webdav/", headers={"Depth": "1"})
        assert resp.status_code == 207
        hrefs = [
            element.text
            for element in ET.fromstring(resp.content).iter("{DAV:}href")
        ]
        assert "/webdav/" in hrefs
        assert "/webdav/hello.txt" in hrefs

        resp = await h.put("/webdav/new.txt", content=b"new")
        assert resp.status_code >= 400


@pytest.mark.asyncio
async def test_webdav_hrefs_include_custom_mount_path(tmp_path: Path) -> None:
    import httpx
    import xml.etree.ElementTree as ET

    from rcm.server import build_http_app

    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "a.txt").write_text("hello", encoding="utf-8")
    cfg = Config(
        server=ServerSpec(public_base_url="http://testserver"),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[],
        webdav=WebDAVSpec(
            root=str(tmp_path),
            path="/files/",
            auth=WebDAVAuthSpec(type="none"),
        ),
    )
    mcp = build_server(cfg, Store(tmp_path / "runs", "http://testserver"), "key")
    app = build_http_app(mcp, cfg, "key")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as h:
        resp = await h.request("PROPFIND", "/files/test/a.txt")

    assert resp.status_code == 207
    hrefs = [
        element.text for element in ET.fromstring(resp.content).iter("{DAV:}href")
    ]
    assert hrefs == ["/files/test/a.txt"]


@pytest.mark.asyncio
async def test_webdav_bearer_auth(tmp_path: Path) -> None:
    import httpx

    from rcm.server import build_http_app

    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    cfg = Config(
        server=ServerSpec(public_base_url="http://testserver"),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[],
        webdav=WebDAVSpec(root=str(tmp_path)),
    )
    mcp = build_server(cfg, Store(tmp_path / "runs", "http://testserver"), "key")
    app = build_http_app(mcp, cfg, "key")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as h:
        resp = await h.get("/webdav/hello.txt")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"

        resp = await h.get(
            "/webdav/hello.txt", headers={"Authorization": "Bearer key"}
        )
        assert resp.status_code == 200
        assert resp.text == "hello"


@pytest.mark.asyncio
async def test_webdav_basic_auth(tmp_path: Path) -> None:
    import httpx

    from rcm.server import build_http_app

    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    cfg = Config(
        server=ServerSpec(public_base_url="http://testserver"),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[],
        webdav=WebDAVSpec(
            root=str(tmp_path),
            auth=WebDAVAuthSpec(type="basic", username="alice", password="secret"),
        ),
    )
    mcp = build_server(cfg, Store(tmp_path / "runs", "http://testserver"), "key")
    app = build_http_app(mcp, cfg, "key")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as h:
        resp = await h.get("/webdav/hello.txt")
        assert resp.status_code == 401

        resp = await h.get(
            "/webdav/hello.txt", auth=httpx.BasicAuth("alice", "secret")
        )
        assert resp.status_code == 200
        assert resp.text == "hello"
