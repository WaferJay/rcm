"""WebDAV application construction and HTTP authentication adapters."""

from __future__ import annotations

import os
import secrets
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from starlette.responses import PlainTextResponse

from .config import WebDAVHideSpec, WebDAVSpec


class WebDAVError(ValueError):
    """Raised when the configured WebDAV share cannot be served."""


def _path_parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.strip("/").split("/") if part)


def _glob_match(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return _glob_match(pattern[1:], path) or bool(path) and _glob_match(
            pattern, path[1:]
        )
    return bool(path) and fnmatchcase(path[0], pattern[0]) and _glob_match(
        pattern[1:], path[1:]
    )


def _glob_hides_path(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if pattern == ("**",):
        return bool(path)
    for length in range(1, len(path) + 1):
        prefix = path[:length]
        if _glob_match(pattern, prefix):
            return True
        if pattern[-1:] == ("**",) and _glob_match(pattern[:-1], prefix):
            return True
    return False


def _filtered_filesystem_provider(
    root: Path,
    readonly: bool,
    hide: WebDAVHideSpec,
    config_path: Path | None,
):
    """Build a WsgiDAV filesystem provider with hidden-resource filtering."""
    from wsgidav import util
    from wsgidav.dav_error import DAVError, HTTP_NOT_FOUND
    from wsgidav.fs_dav_provider import (
        FileResource,
        FilesystemProvider,
        FolderResource,
    )

    root_path = root.resolve()
    hidden_config_parts: tuple[str, ...] | None = None
    if config_path is not None:
        try:
            hidden_config_parts = _path_parts(
                str(config_path.resolve().relative_to(root_path))
            )
        except ValueError:
            pass

    glob_patterns = tuple(tuple(pattern.split("/")) for pattern in hide.glob)

    class FilteredFolderResource(FolderResource):
        def get_member_names(self):
            names = super().get_member_names()
            return [
                name
                for name in names
                if not self.provider._is_hidden_path(util.join_uri(self.path, name))
            ]

    class FilteredFilesystemProvider(FilesystemProvider):
        def __init__(self):
            super().__init__(str(root_path), readonly=readonly)
            self._hide_rcm = hide.rcm
            self._hide_config = hide.config
            self._hidden_config_parts = hidden_config_parts
            self._glob_patterns = glob_patterns

        def _is_hidden_path(self, path: str) -> bool:
            parts = _path_parts(path)
            if not parts:
                return False
            if self._hide_rcm and ".rcm" in parts:
                return True
            if self._hide_config and parts == self._hidden_config_parts:
                return True
            return any(
                _glob_hides_path(pattern, parts)
                for pattern in self._glob_patterns
            )

        def _loc_to_file_path(self, path: str, environ: dict | None = None):
            if self._is_hidden_path(path):
                raise DAVError(HTTP_NOT_FOUND)
            return super()._loc_to_file_path(path, environ)

        def get_resource_inst(self, path: str, environ: dict):
            if self._is_hidden_path(path):
                return None
            resource = super().get_resource_inst(path, environ)
            if isinstance(resource, FolderResource):
                return FilteredFolderResource(path, environ, resource._file_path)
            if isinstance(resource, FileResource):
                return resource
            return None

    return FilteredFilesystemProvider()


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


def _wsgidav_config(
    spec: WebDAVSpec, config_path: Path | None = None
) -> dict[str, Any]:
    auth = spec.auth
    config: dict[str, Any] = {
        # WsgiDAV uses this value when constructing resource hrefs. The ASGI
        # mount already supplies the same prefix as WSGI SCRIPT_NAME, but the
        # provider's mount_path is configured independently by WsgiDAV.
        "mount_path": spec.path.rstrip("/"),
        "provider_mapping": {
            "/": _filtered_filesystem_provider(
                resolve_webdav_root(spec),
                spec.read_only,
                spec.hide,
                config_path,
            )
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


def build_webdav_app(
    spec: WebDAVSpec, api_key: str, config_path: Path | None = None
):
    """Build the ASGI app serving one configured WebDAV share."""
    try:
        from a2wsgi import WSGIMiddleware
        from wsgidav.wsgidav_app import WsgiDAVApp
    except ImportError as exc:
        raise WebDAVError(
            "WebDAV requires the `wsgidav` and `a2wsgi` packages"
        ) from exc

    app = WSGIMiddleware(WsgiDAVApp(_wsgidav_config(spec, config_path)))
    if spec.auth.type == "bearer":
        return BearerAuthMiddleware(app, api_key)
    return app
