"""Validated artifact transfer strategies for proxied RCM results."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import unquote_to_bytes, urlsplit

import httpx

from ..artifacts import ArtifactDescriptor, ArtifactError
from ..tunnel import ArtifactRoute, OriginLockedAsyncTransport, TunnelError


def _file_path_from_uri(uri: str) -> str:
    try:
        parsed = urlsplit(uri)
    except ValueError as exc:
        raise ArtifactError("artifact has an invalid file URI") from exc
    if parsed.scheme.lower() != "file":
        raise ArtifactError("artifact URI is not a file URI")
    if parsed.netloc or parsed.query or parsed.fragment:
        raise ArtifactError("file artifact URI cannot contain a host, query, or fragment")
    if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path):
        raise ArtifactError("file artifact URI contains invalid percent encoding")
    raw_path = unquote_to_bytes(parsed.path)
    if b"\x00" in raw_path:
        raise ArtifactError("file artifact URI contains a NUL byte")
    path = os.fsdecode(raw_path)
    if not Path(path).is_absolute():
        raise ArtifactError("file artifact URI must contain an absolute path")
    return path


def _validate_transfer(
    descriptor: ArtifactDescriptor, size: int, digest: str
) -> None:
    if size != descriptor.bytes:
        raise ArtifactError(
            f"artifact size mismatch: expected {descriptor.bytes}, received {size}"
        )
    if digest != descriptor.sha256:
        raise ArtifactError("artifact SHA-256 mismatch")


def _copy_local_file(
    source: str, destination: Path, descriptor: ArtifactDescriptor
) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        source_stream = open(source, "rb")
    except OSError as exc:
        raise ArtifactError(f"failed to open local artifact {source!r}: {exc}") from exc
    with source_stream, destination.open("wb") as output:
        while chunk := source_stream.read(1024 * 1024):
            size += len(chunk)
            if size > descriptor.bytes:
                raise ArtifactError(
                    f"artifact exceeds declared size of {descriptor.bytes} bytes"
                )
            digest.update(chunk)
            output.write(chunk)
    _validate_transfer(descriptor, size, digest.hexdigest())


async def _read_ssh_diagnostics(stream: asyncio.StreamReader) -> bytes:
    captured = bytearray()
    while chunk := await stream.read(64 * 1024):
        remaining = (64 * 1024) - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return bytes(captured)


async def _copy_ssh_file(
    host: str,
    source: str,
    destination: Path,
    descriptor: ArtifactDescriptor,
) -> None:
    command = f"cat -- {shlex.quote(source)}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            host,
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ArtifactError(
            f"failed to execute ssh for artifact on {host!r}: {exc}"
        ) from exc

    assert proc.stdout is not None
    assert proc.stderr is not None
    diagnostics_task = asyncio.create_task(_read_ssh_diagnostics(proc.stderr))
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("wb") as output:
            while chunk := await proc.stdout.read(1024 * 1024):
                size += len(chunk)
                if size > descriptor.bytes:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    raise ArtifactError(
                        f"artifact exceeds declared size of {descriptor.bytes} bytes"
                    )
                digest.update(chunk)
                output.write(chunk)
        returncode = await proc.wait()
        diagnostics = await diagnostics_task
    except BaseException:
        if not diagnostics_task.done():
            diagnostics_task.cancel()
        try:
            await diagnostics_task
        except BaseException:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except BaseException:
            pass
        raise

    if returncode != 0:
        detail = diagnostics.decode(errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ArtifactError(
            f"failed to read remote artifact {source!r} from {host!r} "
            f"(exit code {returncode}){suffix}"
        )
    _validate_transfer(descriptor, size, digest.hexdigest())


async def _copy_http_file(
    destination: Path,
    descriptor: ArtifactDescriptor,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    try:
        parsed = urlsplit(descriptor.uri)
    except ValueError as exc:
        raise ArtifactError("HTTP artifact URI is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ArtifactError("HTTP artifact URI must be an absolute http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise ArtifactError("HTTP artifact URI cannot contain user information")

    digest = hashlib.sha256()
    size = 0
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=5,
            timeout=httpx.Timeout(30.0),
            transport=transport,
        ) as client:
            async with client.stream(
                "GET", descriptor.uri, headers={"Accept-Encoding": "identity"}
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise ArtifactError(
                        f"artifact download returned HTTP {response.status_code}"
                    )
                if response.url.scheme.lower() not in {"http", "https"}:
                    raise ArtifactError("artifact redirect used a non-HTTP URL")
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > descriptor.bytes:
                            raise ArtifactError(
                                "artifact exceeds declared size of "
                                f"{descriptor.bytes} bytes"
                            )
                        digest.update(chunk)
                        output.write(chunk)
    except ArtifactError:
        raise
    except httpx.HTTPError as exc:
        raise ArtifactError(f"artifact download failed: {exc}") from exc
    _validate_transfer(descriptor, size, digest.hexdigest())


class ArtifactFetcher(Protocol):
    """Copy a validated remote artifact into local staging storage."""

    async def copy(
        self,
        artifact: str,
        run_id: str,
        descriptor: ArtifactDescriptor,
        destination: Path,
    ) -> None: ...


class DirectHttpArtifactFetcher:
    async def copy(
        self,
        artifact: str,
        run_id: str,
        descriptor: ArtifactDescriptor,
        destination: Path,
    ) -> None:
        await _copy_http_file(destination, descriptor)


class TunneledHttpArtifactFetcher:
    def __init__(self, route: ArtifactRoute, socket_path: Path) -> None:
        self._route = route
        self._socket_path = socket_path

    async def copy(
        self,
        artifact: str,
        run_id: str,
        descriptor: ArtifactDescriptor,
        destination: Path,
    ) -> None:
        try:
            uri = self._route.artifact_url(
                descriptor.uri,
                run_id=run_id,
                artifact=artifact,
            )
        except TunnelError as exc:
            raise ArtifactError(str(exc)) from exc
        internal = replace(descriptor, uri=uri)
        await _copy_http_file(
            destination,
            internal,
            transport=OriginLockedAsyncTransport(
                self._route.internal_origin,
                self._socket_path,
                tls_server_name=self._route.tls_server_name,
            ),
        )


class LocalFileArtifactFetcher:
    async def copy(
        self,
        artifact: str,
        run_id: str,
        descriptor: ArtifactDescriptor,
        destination: Path,
    ) -> None:
        source = _file_path_from_uri(descriptor.uri)
        await asyncio.to_thread(_copy_local_file, source, destination, descriptor)


class SSHFileArtifactFetcher:
    def __init__(self, host: str) -> None:
        self._host = host

    async def copy(
        self,
        artifact: str,
        run_id: str,
        descriptor: ArtifactDescriptor,
        destination: Path,
    ) -> None:
        source = _file_path_from_uri(descriptor.uri)
        await _copy_ssh_file(self._host, source, destination, descriptor)


class SchemeArtifactFetcher:
    """Dispatch artifact schemes to injected fetch strategies."""

    def __init__(self, fetchers: Mapping[str, ArtifactFetcher]) -> None:
        self._fetchers = dict(fetchers)

    async def copy(
        self,
        artifact: str,
        run_id: str,
        descriptor: ArtifactDescriptor,
        destination: Path,
    ) -> None:
        try:
            scheme = urlsplit(descriptor.uri).scheme.lower()
        except ValueError as exc:
            raise ArtifactError("artifact URI is invalid") from exc
        fetcher = self._fetchers.get(scheme)
        if fetcher is None:
            raise ArtifactError(f"unsupported artifact URI scheme {scheme!r}")
        await fetcher.copy(artifact, run_id, descriptor, destination)


def _standard_artifact_fetcher(
    transport: str,
    ssh_host: str | None = None,
) -> ArtifactFetcher:
    http = DirectHttpArtifactFetcher()
    fetchers: dict[str, ArtifactFetcher] = {"http": http, "https": http}
    if transport == "stdio":
        fetchers["file"] = LocalFileArtifactFetcher()
    elif transport == "ssh" and ssh_host is not None:
        fetchers["file"] = SSHFileArtifactFetcher(ssh_host)
    return SchemeArtifactFetcher(fetchers)
