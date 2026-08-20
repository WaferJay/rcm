"""Tests for TLS certificate preparation and uvicorn integration."""

from __future__ import annotations

import asyncio
import os
import socket
import ssl
import sys
from pathlib import Path

import httpx
import pytest
from cryptography import x509

from rcm.config import (
    AuthSpec,
    CommandSpec,
    Config,
    DefaultsSpec,
    ServerSpec,
    TLSConfig,
)
from rcm.server import _effective_tls_config, build_server
from rcm.store import Store
from rcm.tls import TLSConfigError, prepare_tls, uvicorn_tls_config


def test_auto_generated_certificate_contains_configured_sans(tmp_path: Path) -> None:
    config_path = tmp_path / "commands.yaml"
    config_path.write_text("commands: []", encoding="utf-8")
    files = prepare_tls(
        config_path,
        TLSConfig(enabled=True, auto_generate=True, hostnames=["internal.example"]),
        "https://public.example:8443/mcp",
    )

    assert files is not None
    assert files.cert_file == tmp_path / ".rcm" / "rcm-cert.pem"
    assert files.key_file == tmp_path / ".rcm" / "rcm-key.pem"
    assert os.stat(files.key_file).st_mode & 0o777 == 0o600

    certificate = x509.load_pem_x509_certificate(files.cert_file.read_bytes())
    san = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert san.get_values_for_type(x509.DNSName) == [
        "public.example",
        "internal.example",
        "localhost",
    ]
    assert san.get_values_for_type(x509.IPAddress)[0].compressed == "127.0.0.1"


def test_auto_generated_certificate_is_reused(tmp_path: Path) -> None:
    config_path = tmp_path / "commands.yaml"
    config_path.write_text("commands: []", encoding="utf-8")
    config = TLSConfig(enabled=True, auto_generate=True, hostnames=["example.test"])

    first = prepare_tls(config_path, config, None)
    assert first is not None
    certificate_bytes = first.cert_file.read_bytes()
    key_bytes = first.key_file.read_bytes()

    second = prepare_tls(config_path, config, None)
    assert second is not None
    assert second.cert_file.read_bytes() == certificate_bytes
    assert second.key_file.read_bytes() == key_bytes


def test_auto_generation_requires_hostname_without_public_url(tmp_path: Path) -> None:
    config_path = tmp_path / "commands.yaml"
    config_path.write_text("commands: []", encoding="utf-8")

    with pytest.raises(TLSConfigError, match="requires server.public_base_url"):
        prepare_tls(
            config_path,
            TLSConfig(enabled=True, auto_generate=True),
            None,
        )


def test_uvicorn_tls_config_uses_resolved_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "commands.yaml"
    config_path.write_text("commands: []", encoding="utf-8")
    files = prepare_tls(
        config_path,
        TLSConfig(enabled=True, auto_generate=True, hostnames=["example.test"]),
        None,
    )
    assert files is not None
    assert uvicorn_tls_config(files) == {
        "ssl_certfile": str(files.cert_file),
        "ssl_keyfile": str(files.key_file),
    }
    assert uvicorn_tls_config(None) == {}


def test_manual_certificate_paths_are_relative_to_config(tmp_path: Path) -> None:
    config_path = tmp_path / "commands.yaml"
    config_path.write_text("commands: []", encoding="utf-8")
    generated = prepare_tls(
        config_path,
        TLSConfig(enabled=True, auto_generate=True, hostnames=["example.test"]),
        None,
    )
    assert generated is not None

    files = prepare_tls(
        config_path,
        TLSConfig(
            enabled=True,
            cert_file=".rcm/rcm-cert.pem",
            key_file=".rcm/rcm-key.pem",
        ),
        None,
    )
    assert files == generated


def test_tls_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCM_TLS_ENABLED", "true")
    monkeypatch.setenv("RCM_TLS_AUTO_GENERATE", "1")
    monkeypatch.setenv("RCM_TLS_CERT_FILE", "cert.pem")
    monkeypatch.setenv("RCM_TLS_KEY_FILE", "key.pem")
    monkeypatch.setenv("RCM_TLS_HOSTNAMES", "one.example, 192.0.2.1")

    config = _effective_tls_config(TLSConfig())

    assert config.enabled is True
    assert config.auto_generate is True
    assert config.cert_file == "cert.pem"
    assert config.key_file == "key.pem"
    assert config.hostnames == ["one.example", "192.0.2.1"]


@pytest.mark.asyncio
async def test_fastmcp_serves_healthz_over_https(tmp_path: Path) -> None:
    config_path = tmp_path / "commands.yaml"
    config_path.write_text("commands: []", encoding="utf-8")
    files = prepare_tls(
        config_path,
        TLSConfig(enabled=True, auto_generate=True, hostnames=["127.0.0.1"]),
        "https://127.0.0.1",
    )
    assert files is not None

    port = _free_port()
    cfg = Config(
        server=ServerSpec(
            host="127.0.0.1",
            port=port,
            public_base_url=f"https://127.0.0.1:{port}",
            tls=TLSConfig(enabled=True),
        ),
        auth=AuthSpec(api_key=None),
        defaults=DefaultsSpec(),
        commands=[
            CommandSpec(
                name="noop",
                description="Do nothing.",
                command=[sys.executable, "-c", "pass"],
            )
        ],
    )
    mcp = build_server(
        cfg,
        Store(tmp_path / "runs", f"https://127.0.0.1:{port}"),
        "key",
    )

    async def serve() -> None:
        await mcp.run_async(
            transport="http",
            host="127.0.0.1",
            port=port,
            log_level="warning",
            uvicorn_config=uvicorn_tls_config(files),
        )

    task = asyncio.create_task(serve())
    try:
        await _wait_for_port(port)
        tls_context = ssl.create_default_context(cafile=str(files.cert_file))
        async with httpx.AsyncClient(verify=tls_context) as client:
            response = await client.get(f"https://127.0.0.1:{port}/healthz")
        assert response.status_code == 200
        assert response.text == "ok"
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for_port(port: int) -> None:
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError("HTTPS server failed to start")
