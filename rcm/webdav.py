"""WebDAV application construction and HTTP authentication adapters."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from starlette.responses import PlainTextResponse

from .config import WebDAVSpec


class WebDAVError(ValueError):
    """Raised when the configured WebDAV share cannot be served."""


def resolve_webdav_root(spec: WebDAVSpec) -> Path:
    """Resolve and validate the configured WebDAV root directory."""
    root = Path(spec.root).expanduser().resolve()
    if not root.exists():
        raise WebDAVError(f"webdav.root does not exist: {root}")
    if not root.is_dir():
        raise WebDAVError(f"webdav.root is not a directory: {root}")
    if not os.access(root, os.R_OK | os.X_OK):
        raise WebDAVError(f"webdav.root is not readable: {root}")
    if not spec.read_only and not os.access(root, os.W_OK | os.X_OK):
        raise WebDAVError(f"webdav.root is not writable: {root}")
    return root


class BearerAuthMiddleware:
    """Protect one ASGI application with a bearer token."""

    def __init__(self, app: Any, expected: str) -> None:
        if not expected:
            raise ValueError("bearer token must be non-empty")
        self.app = app
        self.expected = expected

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        authorization = dict(scope.get("headers", [])).get(b"authorization", b"")
        try:
            presented = authorization.decode("ascii")
        except UnicodeDecodeError:
            presented = ""
        if not presented.lower().startswith("bearer ") or not secrets.compare_digest(
            presented[7:].strip(), self.expected
        ):
            response = PlainTextResponse(
                "unauthorized\n",
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _wsgidav_config(spec: WebDAVSpec) -> dict[str, Any]:
    auth = spec.auth
    config: dict[str, Any] = {
        # WsgiDAV uses this value when constructing resource hrefs. The ASGI
        # mount already supplies the same prefix as WSGI SCRIPT_NAME, but the
        # provider's mount_path is configured independently by WsgiDAV.
        "mount_path": spec.path.rstrip("/"),
        "provider_mapping": {
            "/": {"root": str(resolve_webdav_root(spec)), "readonly": spec.read_only}
        },
        "verbose": 1,
        "http_authenticator": {
            "accept_basic": auth.type == "basic",
            "accept_digest": False,
            "default_to_digest": False,
        },
    }

    if auth.type == "basic":
        password = os.environ.get("RCM_WEBDAV_PASSWORD") or auth.password
        if not auth.username or not password:
            raise WebDAVError("Basic WebDAV authentication credentials are missing")
        config["simple_dc"] = {
            "user_mapping": {
                "*": {auth.username: {"password": password}},
            }
        }
    else:
        # Bearer authentication is enforced by the ASGI adapter. WsgiDAV must
        # still see an anonymous request after the adapter has authorized it.
        config["simple_dc"] = {"user_mapping": {"*": True}}

    return config


def build_webdav_app(spec: WebDAVSpec, api_key: str):
    """Build the ASGI app serving one configured WebDAV share."""
    try:
        from a2wsgi import WSGIMiddleware
        from wsgidav.wsgidav_app import WsgiDAVApp
    except ImportError as exc:
        raise WebDAVError(
            "WebDAV requires the `wsgidav` and `a2wsgi` packages"
        ) from exc

    app = WSGIMiddleware(WsgiDAVApp(_wsgidav_config(spec)))
    if spec.auth.type == "bearer":
        return BearerAuthMiddleware(app, api_key)
    return app
