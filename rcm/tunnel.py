"""POSIX OpenSSH tunnels and origin-locked HTTP routing."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx


SSH_TUNNEL_START_TIMEOUT = 15.0
SSH_TUNNEL_STOP_TIMEOUT = 5.0
SSH_DIAGNOSTIC_LIMIT = 64 * 1024


class TunnelError(RuntimeError):
    """Raised when a tunnel cannot be configured or kept running."""


class TunnelRouteError(httpx.TransportError):
    """Raised when a request attempts to leave its configured tunnel origin."""


@dataclass(frozen=True)
class HttpOrigin:
    scheme: str
    host: str
    port: int

    @classmethod
    def from_url(cls, url: str | httpx.URL) -> HttpOrigin:
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, ValueError) as exc:
            raise TunnelError(
                "URL must be a valid absolute HTTP or HTTPS URL"
            ) from exc
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.host:
            raise TunnelError("URL must contain an absolute HTTP or HTTPS origin")
        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80
        return cls(scheme=scheme, host=parsed.host.lower(), port=port)


@dataclass(frozen=True)
class RemoteHttpBinding:
    """The RCM HTTP listener as reached from the SSH server."""

    scheme: str
    host: str
    port: int

    @property
    def origin(self) -> HttpOrigin:
        return HttpOrigin.from_url(self.base_url)

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def forward_target(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def derive_remote_http_binding(
    host: str,
    port: int,
    *,
    tls_enabled: bool,
) -> RemoteHttpBinding:
    """Convert an HTTP bind address into a safe SSH-side connection address."""
    if not isinstance(host, str):
        raise TunnelError("remote HTTP host must be a string")
    normalized = host.strip()
    if normalized == "0.0.0.0":
        normalized = "127.0.0.1"
    elif normalized in {"::", "[::]"}:
        normalized = "::1"
    elif normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    if not normalized:
        raise TunnelError("remote HTTP host must be non-empty")
    if any(not character.isprintable() for character in normalized) or any(
        character.isspace() or character in "/[]\\" for character in normalized
    ):
        raise TunnelError("remote HTTP host contains invalid characters")
    if ":" in normalized:
        try:
            parsed_ip = ipaddress.ip_address(normalized)
        except ValueError as exc:
            raise TunnelError("remote HTTP host is not a valid IPv6 address") from exc
        if parsed_ip.version != 6:
            raise TunnelError("remote HTTP host is not a valid IPv6 address")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise TunnelError("remote HTTP port must be an integer between 1 and 65535")
    if not isinstance(tls_enabled, bool):
        raise TunnelError("remote TLS enabled flag must be a boolean")
    return RemoteHttpBinding(
        scheme="https" if tls_enabled else "http",
        host=normalized,
        port=port,
    )


@dataclass(frozen=True)
class ArtifactRoute:
    """Translate one RCM's public artifact URLs to its internal HTTP listener."""

    public_base_url: str
    internal_base_url: str
    internal_origin: HttpOrigin
    tls_server_name: str

    @classmethod
    def create(
        cls,
        public_base_url: str,
        binding: RemoteHttpBinding,
    ) -> ArtifactRoute:
        try:
            parsed = urlsplit(public_base_url)
        except ValueError as exc:
            raise TunnelError("public_base_url is invalid") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or parsed.hostname is None
        ):
            raise TunnelError("public_base_url must be an absolute HTTP or HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise TunnelError("public_base_url must not contain user information")
        if parsed.query or parsed.fragment:
            raise TunnelError("public_base_url must not contain a query or fragment")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise TunnelError("public_base_url contains an invalid port") from exc
        return cls(
            public_base_url=public_base_url.rstrip("/"),
            internal_base_url=binding.base_url,
            internal_origin=binding.origin,
            tls_server_name=parsed.hostname,
        )

    @property
    def mcp_endpoint(self) -> str:
        return f"{self.internal_base_url}/mcp"

    def artifact_url(self, uri: str, *, run_id: str, artifact: str) -> str:
        expected = f"{self.public_base_url}/runs/{run_id}/{artifact}"
        if uri != expected:
            raise TunnelError(
                f"tunneled artifact URL must match {expected!r}, got {uri!r}"
            )
        return f"{self.internal_base_url}/runs/{run_id}/{artifact}"


class OriginLockedAsyncTransport(httpx.AsyncBaseTransport):
    """Route one logical HTTP origin over a fixed Unix-domain socket."""

    def __init__(
        self,
        origin: HttpOrigin,
        socket_path: Path,
        *,
        tls_server_name: str | None = None,
    ) -> None:
        self._origin = origin
        self._tls_server_name = tls_server_name
        self._delegate = httpx.AsyncHTTPTransport(uds=str(socket_path))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            actual = HttpOrigin.from_url(request.url)
        except TunnelError as exc:
            raise TunnelRouteError(str(exc)) from exc
        if actual != self._origin:
            raise TunnelRouteError(
                "request origin does not match the SSH tunnel destination"
            )
        if actual.scheme == "https" and self._tls_server_name is not None:
            # The socket reaches an internal address, while the certificate
            # normally identifies the externally advertised RCM hostname.
            request.extensions["sni_hostname"] = self._tls_server_name
        return await self._delegate.handle_async_request(request)

    async def aclose(self) -> None:
        await self._delegate.aclose()


def uds_http_client_factory(
    origin: HttpOrigin,
    socket_path: Path,
    *,
    tls_server_name: str | None = None,
) -> Callable[..., httpx.AsyncClient]:
    """Build the FastMCP-compatible HTTP client factory for a tunnel."""

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=OriginLockedAsyncTransport(
                origin,
                socket_path,
                tls_server_name=tls_server_name,
            ),
            **kwargs,
        )

    return factory


def build_ssh_tunnel_command(
    ssh_host: str,
    socket_path: Path,
    binding: RemoteHttpBinding,
) -> list[str]:
    if ":" in str(socket_path):
        raise TunnelError("SSH tunnel socket path must not contain a colon")
    return [
        "ssh",
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        f"{socket_path}:{binding.forward_target}",
        ssh_host,
    ]


async def _capture_diagnostics(stream: asyncio.StreamReader | None) -> bytes:
    captured = bytearray()
    if stream is None:
        return bytes(captured)
    while chunk := await stream.read(64 * 1024):
        remaining = SSH_DIAGNOSTIC_LIMIT - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return bytes(captured)


class SSHTunnel:
    """Own a long-lived OpenSSH Unix-socket local forward."""

    def __init__(self, ssh_host: str, binding: RemoteHttpBinding) -> None:
        self.ssh_host = ssh_host
        self.binding = binding
        self.socket_path: Path | None = None
        self._temp_dir: Path | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._diagnostics_task: asyncio.Task[bytes] | None = None

    async def __aenter__(self) -> SSHTunnel:
        if os.name != "posix":
            raise TunnelError("SSH HTTP tunnels currently require a POSIX platform")
        try:
            temp_root = "/tmp" if Path("/tmp").is_dir() else None
            self._temp_dir = Path(
                tempfile.mkdtemp(prefix="rcm-ssh-", dir=temp_root)
            )
            self._temp_dir.chmod(0o700)
            self.socket_path = self._temp_dir / "http.sock"
            command = build_ssh_tunnel_command(
                self.ssh_host,
                self.socket_path,
                self.binding,
            )
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise TunnelError(
                    f"failed to execute ssh for tunnel to {self.ssh_host!r}: {exc}"
                ) from exc
            self._diagnostics_task = asyncio.create_task(
                _capture_diagnostics(self._process.stderr)
            )
            await self._wait_until_ready()
            return self
        except TunnelError:
            await self.aclose()
            raise
        except OSError as exc:
            await self.aclose()
            raise TunnelError(
                f"failed to prepare SSH tunnel to {self.ssh_host!r}: {exc}"
            ) from exc
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _wait_until_ready(self) -> None:
        if self.socket_path is None or self._process is None:
            raise TunnelError("SSH tunnel startup state is incomplete")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SSH_TUNNEL_START_TIMEOUT
        while not self.socket_path.exists():
            if self._process.returncode is not None:
                raise TunnelError(await self._failure_message("exited during startup"))
            if loop.time() >= deadline:
                raise TunnelError(
                    f"SSH tunnel to {self.ssh_host!r} did not become ready within "
                    f"{SSH_TUNNEL_START_TIMEOUT:g} seconds"
                )
            await asyncio.sleep(0.05)

    async def wait(self) -> None:
        """Wait until the SSH process exits, then report it as a tunnel failure."""
        if self._process is None:
            raise TunnelError("SSH tunnel has not been started")
        await self._process.wait()
        raise TunnelError(await self._failure_message("exited unexpectedly"))

    async def _failure_message(self, event: str) -> str:
        if self._process is None:
            return f"SSH tunnel to {self.ssh_host!r} {event}"
        diagnostics = b""
        if self._diagnostics_task is not None:
            diagnostics = await self._diagnostics_task
        detail = diagnostics.decode(errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        return (
            f"SSH tunnel to {self.ssh_host!r} {event} "
            f"(exit code {self._process.returncode}){suffix}"
        )

    async def aclose(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=SSH_TUNNEL_STOP_TIMEOUT
                )
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if self._diagnostics_task is not None:
            await asyncio.gather(self._diagnostics_task, return_exceptions=True)
        if self._temp_dir is not None:
            await asyncio.to_thread(shutil.rmtree, self._temp_dir, True)
        self._process = None
        self._diagnostics_task = None
        self.socket_path = None
        self._temp_dir = None
