"""TLS certificate preparation for the rcm HTTP server."""

from __future__ import annotations

import ipaddress
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .config import TLSConfig


class TLSConfigError(ValueError):
    """Raised when TLS files or TLS certificate configuration are invalid."""


@dataclass(frozen=True)
class TLSFiles:
    cert_file: Path
    key_file: Path


def _resolve_path(config_path: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path


def _public_hostname(public_base_url: str | None) -> str | None:
    if not public_base_url:
        return None
    hostname = urlsplit(public_base_url).hostname
    if hostname is None:
        raise TLSConfigError(
            "server.public_base_url must contain a hostname when generating "
            "a TLS certificate"
        )
    return hostname


def _certificate_hostnames(
    hostnames: list[str], public_base_url: str | None
) -> list[str]:
    public_hostname = _public_hostname(public_base_url)
    if public_hostname is None and not hostnames:
        raise TLSConfigError(
            "TLS certificate generation requires server.public_base_url or "
            "server.tls.hostnames"
        )

    resolved: list[str] = []
    for hostname in [
        public_hostname,
        *hostnames,
        "localhost",
        "127.0.0.1",
    ]:
        if hostname is not None and hostname not in resolved:
            resolved.append(hostname)
    return resolved


def _san_entry(hostname: str) -> x509.GeneralName:
    normalized = hostname.strip("[]")
    try:
        return x509.IPAddress(ipaddress.ip_address(normalized))
    except ValueError:
        return x509.DNSName(hostname)


def _generate_certificate(hostnames: list[str]) -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName(
                [_san_entry(hostname) for hostname in hostnames]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    cert_bytes = certificate.public_bytes(serialization.Encoding.PEM)
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return cert_bytes, key_bytes


def _write_generated_files(
    cert_file: Path, key_file: Path, hostnames: list[str]
) -> None:
    cert_bytes, key_bytes = _generate_certificate(hostnames)
    cert_tmp = cert_file.with_name(f".{cert_file.name}.{secrets.token_hex(8)}.tmp")
    key_tmp = key_file.with_name(f".{key_file.name}.{secrets.token_hex(8)}.tmp")
    try:
        cert_tmp.write_bytes(cert_bytes)
        key_tmp.write_bytes(key_bytes)
        os.chmod(cert_tmp, 0o644)
        os.chmod(key_tmp, 0o600)
        if cert_file.exists() or key_file.exists():
            raise TLSConfigError(
                "generated TLS files appeared while preparing the certificate"
            )
        cert_tmp.replace(cert_file)
        key_tmp.replace(key_file)
    finally:
        cert_tmp.unlink(missing_ok=True)
        key_tmp.unlink(missing_ok=True)


def _validate_files(cert_file: Path, key_file: Path) -> TLSFiles:
    if not cert_file.is_file():
        raise TLSConfigError(f"TLS certificate file not found: {cert_file}")
    if not key_file.is_file():
        raise TLSConfigError(f"TLS private key file not found: {key_file}")
    return TLSFiles(cert_file=cert_file, key_file=key_file)


def prepare_tls(
    config_path: str | Path,
    config: TLSConfig,
    public_base_url: str | None,
) -> TLSFiles | None:
    """Resolve or generate the certificate files required by the server."""
    if not config.enabled:
        return None

    config_path = Path(config_path).resolve()
    if (config.cert_file is None) != (config.key_file is None):
        raise TLSConfigError(
            "server.tls.cert_file and server.tls.key_file must be provided together"
        )

    if config.cert_file is not None and config.key_file is not None:
        return _validate_files(
            _resolve_path(config_path, config.cert_file),
            _resolve_path(config_path, config.key_file),
        )

    if not config.auto_generate:
        raise TLSConfigError(
            "server.tls requires cert_file/key_file or auto_generate: true"
        )

    tls_dir = config_path.parent / ".rcm"
    tls_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(tls_dir, 0o700)
    cert_file = tls_dir / "rcm-cert.pem"
    key_file = tls_dir / "rcm-key.pem"
    if cert_file.exists() or key_file.exists():
        return _validate_files(cert_file, key_file)

    _write_generated_files(
        cert_file,
        key_file,
        _certificate_hostnames(config.hostnames, public_base_url),
    )
    return _validate_files(cert_file, key_file)


def uvicorn_tls_config(files: TLSFiles | None) -> dict[str, str]:
    """Return uvicorn keyword arguments for the resolved TLS files."""
    if files is None:
        return {}
    return {
        "ssl_certfile": str(files.cert_file),
        "ssl_keyfile": str(files.key_file),
    }
