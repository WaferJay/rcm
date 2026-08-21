"""Build the FastMCP server: dynamic tool registration + public download routes."""

from __future__ import annotations

import asyncio
import argparse
import inspect
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from .auth import ApiKeyAuth
from .config import CommandSpec, Config, ParamSpec, TLSConfig, load_config
from .runner import run_command
from .proxy import ProxyError, ProxyRuntime
from .store import RUN_ID_RE, Store
from .tls import TLSConfigError, prepare_tls, uvicorn_tls_config

PY_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _build_tool_fn(
    spec: CommandSpec, default_timeout: float | None, default_cwd: str | None, store: Store
):
    """Synthesize an `async def <spec.name>(...)` function whose signature matches spec.params."""
    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for p in spec.params:
        py_type = PY_TYPES[p.type]
        default = p.default if p.has_default else inspect.Parameter.empty
        params.append(
            inspect.Parameter(
                name=p.name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=py_type,
            )
        )
        annotations[p.name] = py_type
    annotations["return"] = dict

    async def _impl(**kwargs):
        return await run_command(
            spec,
            kwargs,
            store=store,
            default_timeout=default_timeout,
            default_cwd=default_cwd,
        )

    _impl.__name__ = spec.name
    _impl.__qualname__ = spec.name
    _impl.__doc__ = _format_docstring(spec)
    _impl.__signature__ = inspect.Signature(parameters=params, return_annotation=dict)
    _impl.__annotations__ = annotations
    return _impl


def _format_docstring(spec: CommandSpec) -> str:
    lines = [spec.description.strip(), ""]
    if spec.params:
        lines.append("Parameters:")
        for p in spec.params:
            note: list[str] = []
            if p.has_default:
                note.append(f"default={p.default!r}")
            if p.pattern:
                note.append(f"pattern={p.pattern}")
            if p.enum:
                note.append(f"enum={p.enum}")
            extra = f" ({', '.join(note)})" if note else ""
            desc = f" - {p.description}" if p.description else ""
            lines.append(f"  {p.name} ({p.type}){extra}{desc}")
        lines.append("")
    lines.extend(
        [
            "Returns a dict with: run_id, returncode, timed_out, duration_ms,",
            "stdout_bytes, stderr_bytes, stdout_url, stderr_url.",
            "Stdout and stderr are downloaded from the URLs (no auth needed; the run_id is the secret).",
        ]
    )
    return "\n".join(lines)


def _bad_request(msg: str) -> Response:
    return JSONResponse({"error": msg}, status_code=400)


def _not_found(msg: str = "not found") -> Response:
    return JSONResponse({"error": msg}, status_code=404)


def _serve_log(path: Path, request: Request, content_type: str) -> Response:
    if not path.is_file():
        return _not_found("log file not found")
    tail_raw = request.query_params.get("tail")
    if tail_raw is not None:
        try:
            tail = int(tail_raw)
        except ValueError:
            return _bad_request("`tail` must be a non-negative integer")
        if tail < 0:
            return _bad_request("`tail` must be a non-negative integer")
        size = path.stat().st_size
        offset = max(0, size - tail)
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        return Response(content=data, media_type=content_type)
    return FileResponse(path, media_type=content_type)


def _register_download_routes(mcp: FastMCP, store: Store) -> None:
    @mcp.custom_route("/runs/{run_id}/stdout", methods=["GET"])
    async def stdout_route(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        if not RUN_ID_RE.fullmatch(run_id):
            return _bad_request("invalid run_id")
        return _serve_log(
            store.file_path(run_id, "stdout"),
            request,
            "text/plain; charset=utf-8",
        )

    @mcp.custom_route("/runs/{run_id}/stderr", methods=["GET"])
    async def stderr_route(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        if not RUN_ID_RE.fullmatch(run_id):
            return _bad_request("invalid run_id")
        return _serve_log(
            store.file_path(run_id, "stderr"),
            request,
            "text/plain; charset=utf-8",
        )

    @mcp.custom_route("/runs/{run_id}/meta", methods=["GET"])
    async def meta_route(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        if not RUN_ID_RE.fullmatch(run_id):
            return _bad_request("invalid run_id")
        path = store.file_path(run_id, "meta")
        if not path.is_file():
            return _not_found("meta not found")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _not_found("meta not readable")
        return JSONResponse(data)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return PlainTextResponse("ok")


def _register_command_tools(mcp: FastMCP, cfg: Config, store: Store) -> None:
    for spec in cfg.commands:
        fn = _build_tool_fn(spec, cfg.defaults.timeout, cfg.defaults.cwd, store)
        mcp.tool(fn)


def build_server(cfg: Config, store: Store, api_key: str | None) -> FastMCP:
    mcp: FastMCP = FastMCP("rcm")
    if api_key is not None:
        mcp.add_middleware(ApiKeyAuth(api_key))
    _register_command_tools(mcp, cfg, store)
    _register_download_routes(mcp, store)
    return mcp


async def build_proxy_server(
    cfg: Config, store: Store, api_key: str | None
) -> tuple[FastMCP, ProxyRuntime]:
    """Build a server with local commands and discovered proxy tools."""
    runtime = await ProxyRuntime.create(cfg, api_key, store)
    try:
        _register_command_tools(runtime.server, cfg, store)
        _register_download_routes(runtime.server, store)
    except Exception:
        await runtime.close()
        raise
    return runtime.server, runtime


def build_http_app(mcp: FastMCP, cfg: Config, api_key: str) -> Any:
    """Build the HTTP MCP application."""
    return mcp.http_app()


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _env_path(name: str, default: str | None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    if not value.strip():
        raise ValueError(f"{name} must be a non-empty path")
    return value


def _effective_tls_config(config: TLSConfig) -> TLSConfig:
    hostnames_raw = os.environ.get("RCM_TLS_HOSTNAMES")
    if hostnames_raw is None:
        hostnames = config.hostnames
    else:
        hostnames = [hostname.strip() for hostname in hostnames_raw.split(",")]
        if not hostnames or any(not hostname for hostname in hostnames):
            raise ValueError("RCM_TLS_HOSTNAMES must contain non-empty hostnames")

    return replace(
        config,
        enabled=_env_bool("RCM_TLS_ENABLED", config.enabled),
        cert_file=_env_path("RCM_TLS_CERT_FILE", config.cert_file),
        key_file=_env_path("RCM_TLS_KEY_FILE", config.key_file),
        auto_generate=_env_bool("RCM_TLS_AUTO_GENERATE", config.auto_generate),
        hostnames=hostnames,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the rcm MCP server")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="serve MCP over stdin/stdout instead of Streamable HTTP",
    )
    args = parser.parse_args()

    cfg_path = os.environ.get("RCM_CONFIG", "commands.yaml")
    try:
        cfg = load_config(cfg_path)
    except Exception as e:
        sys.exit(f"rcm: failed to load config {cfg_path}: {e}")

    stdio = args.stdio or cfg.server.transport == "stdio"

    api_key = os.environ.get("RCM_API_KEY") or cfg.auth.api_key
    if not stdio and not api_key:
        sys.exit(
            "rcm: RCM_API_KEY is required (set the env var or auth.api_key in config)"
        )

    runs_dir = Path(os.environ.get("RCM_RUNS_DIR", "./runs")).resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)

    public_base_url = os.environ.get("RCM_PUBLIC_BASE_URL") or cfg.server.public_base_url
    if not stdio and not public_base_url:
        sys.exit(
            "rcm: public base URL is required "
            "(set RCM_PUBLIC_BASE_URL or server.public_base_url)"
        )
    if stdio:
        public_base_url = public_base_url or runs_dir.as_uri()

    tls_files = None
    if not stdio:
        try:
            tls_config = _effective_tls_config(cfg.server.tls)
            if (
                tls_config.enabled
                and urlsplit(public_base_url).scheme.lower() != "https"
            ):
                raise TLSConfigError(
                    "server.public_base_url must use https when TLS is enabled"
                )
            tls_files = prepare_tls(cfg_path, tls_config, public_base_url)
        except (TLSConfigError, ValueError) as e:
            sys.exit(f"rcm: failed to configure TLS: {e}")

    store = Store(
        runs_dir,
        public_base_url=public_base_url,
        local_urls=stdio,
    )

    retention_raw = os.environ.get("RCM_RUNS_RETENTION", "0")
    try:
        retention = int(retention_raw)
    except ValueError:
        retention = 0
    if retention > 0:
        store.prune(retention)

    if stdio:
        if cfg.proxy is None:
            build_server(cfg, store, api_key=None).run(transport="stdio")
            return

        async def serve_proxy_stdio() -> None:
            runtime: ProxyRuntime | None = None
            try:
                mcp, runtime = await build_proxy_server(cfg, store, api_key=None)
                await mcp.run_async(transport="stdio")
            finally:
                if runtime is not None:
                    await runtime.close()

        try:
            asyncio.run(serve_proxy_stdio())
        except ProxyError as e:
            sys.exit(f"rcm: failed to configure proxy: {e}")
        return

    host = os.environ.get("RCM_HOST") or cfg.server.host or "0.0.0.0"
    port = int(os.environ.get("RCM_PORT") or cfg.server.port or 8000)

    uvicorn_config = uvicorn_tls_config(tls_files)
    scheme = "https" if tls_files is not None else "http"

    if cfg.proxy is None:
        mcp = build_server(cfg, store, api_key)
        print(
            f"rcm: serving {len(cfg.commands)} command tool(s) "
            f"on {scheme}://{host}:{port}/mcp/  (public base: {public_base_url})",
            file=sys.stderr,
        )
        mcp.run(
            transport="http",
            host=host,
            port=port,
            uvicorn_config=uvicorn_config,
        )
        return

    print(
        f"rcm: preparing proxy targets on "
        f"{scheme}://{host}:{port}/mcp/  (public base: {public_base_url})",
        file=sys.stderr,
    )

    async def serve_proxy() -> None:
        runtime: ProxyRuntime | None = None
        try:
            mcp, runtime = await build_proxy_server(cfg, store, api_key)
            app = build_http_app(mcp, cfg, api_key)
            import uvicorn

            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=host,
                    port=port,
                    **uvicorn_config,
                )
            )
            await server.serve()
        finally:
            if runtime is not None:
                await runtime.close()

    try:
        asyncio.run(serve_proxy())
    except ProxyError as e:
        sys.exit(f"rcm: failed to configure proxy: {e}")
