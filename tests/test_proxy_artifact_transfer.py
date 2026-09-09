"""Tests for validated proxy artifact transfers."""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from rcm.artifacts import ArtifactDescriptor
from rcm.proxy.artifact_transfer import (
    _copy_http_file,
    _copy_ssh_file,
    _file_path_from_uri,
)


@pytest.mark.asyncio
async def test_http_artifact_downloads_without_mcp_authorization(tmp_path) -> None:
    payload = b"remote over http\x00"

    def handle(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, content=payload)

    descriptor = ArtifactDescriptor(
        uri="https://remote.example/runs/id/stdout",
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    destination = tmp_path / "stdout.log"

    await _copy_http_file(
        destination, descriptor, transport=httpx.MockTransport(handle)
    )

    assert destination.read_bytes() == payload


@pytest.mark.parametrize(
    "uri",
    [
        "file://other-host/var/run/stdout.log",
        "file:relative.log",
        "file:///var/run/stdout.log?tail=1",
        "file:///var/run/bad%ZZ.log",
        "file:///var/run/nul%00.log",
    ],
)
def test_file_artifact_uri_validation(uri: str) -> None:
    with pytest.raises(Exception):
        _file_path_from_uri(uri)


@pytest.mark.asyncio
async def test_ssh_artifact_streams_binary_output_separately_from_diagnostics(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"\x00remote\xff"
    captured: dict = {}

    class FakeProcess:
        returncode = 0

        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(payload)
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"ssh diagnostic that is not artifact data")
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    descriptor = ArtifactDescriptor(
        uri="file:///srv/rcm/run%20files/stdout.log",
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    destination = tmp_path / "stdout.log"

    await _copy_ssh_file(
        "compile-machine",
        _file_path_from_uri(descriptor.uri),
        destination,
        descriptor,
    )

    assert destination.read_bytes() == payload
    assert captured["args"] == (
        "ssh",
        "compile-machine",
        "cat -- '/srv/rcm/run files/stdout.log'",
    )
    assert captured["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL

