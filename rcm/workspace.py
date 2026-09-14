"""Command workspace isolation and request-scoped identifiers.

This module deliberately owns the policy-to-path boundary.  Callers receive a
``Workspace`` value and never need to know how an IP address or MCP session is
represented on disk or in an artifact URL.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import re
from dataclasses import dataclass
from pathlib import Path

from .config.models import IsolationSpec


class WorkspaceError(RuntimeError):
    """Raised when an isolated command cannot establish its workspace."""


@dataclass(frozen=True)
class Workspace:
    """An opaque, per-command workspace selected for one invocation."""

    scope_id: str
    base_dir: Path

    @property
    def cwd(self) -> Path:
        return self.base_dir / self.scope_id

    def ensure(self) -> Path:
        self.cwd.mkdir(parents=True, exist_ok=True)
        return self.cwd


def workspace_base_dir(
    spec: IsolationSpec,
    *,
    command_cwd: str | None = None,
    default_cwd: str | None = None,
) -> Path:
    """Resolve an isolation root with the same omission semantics as ``cwd``."""
    configured = spec.base_dir or command_cwd or default_cwd or os.getcwd()
    return Path(configured).expanduser().resolve()


class ScopeResolver:
    """Resolve command isolation modes from the active FastMCP request."""

    def __init__(self, secret: bytes | None = None) -> None:
        self._secret = secret or secrets.token_bytes(32)

    def resolve(
        self, spec: IsolationSpec | None, *, base_dir: Path | None = None
    ) -> Workspace | None:
        if spec is None or spec.by == "none":
            return None
        root = base_dir or workspace_base_dir(spec)
        forwarded = self._forwarded_scope_id()
        if forwarded is not None:
            scope_id = forwarded
        elif spec.by == "call":
            scope_id = self._new_scope_id()
        elif spec.by == "ip":
            scope_id = self._from_ip()
        elif spec.by == "session":
            scope_id = self._from_session()
        else:  # defensive: config validation owns the public error message
            raise WorkspaceError(f"unsupported isolation mode {spec.by!r}")
        return Workspace(scope_id=scope_id, base_dir=root)

    def _forwarded_scope_id(self) -> str | None:
        """Read an opaque ID forwarded by an RCM proxy, if one is present."""
        try:
            context = self._context()
        except WorkspaceError:
            return None
        request_context = getattr(context, "request_context", None)
        meta = getattr(request_context, "meta", None)
        rcm = self._value(meta, "rcm")
        workspace = self._value(rcm, "workspace")
        scope_id = self._value(workspace, "scope_id")
        if isinstance(scope_id, str) and re.fullmatch(r"scope-[A-Za-z0-9_-]+", scope_id):
            return scope_id
        return None

    @staticmethod
    def _value(value: object, name: str) -> object | None:
        if isinstance(value, dict):
            return value.get(name)
        return getattr(value, name, None)

    def _new_scope_id(self) -> str:
        return f"scope-{secrets.token_urlsafe(32)}"

    def _opaque_scope_id(self, value: str) -> str:
        digest = hmac.new(
            self._secret, value.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"scope-{digest}"

    def _context(self):
        try:
            from fastmcp.server.dependencies import get_context

            return get_context()
        except Exception as exc:  # FastMCP exposes no request context in stdio/CLI.
            raise WorkspaceError("isolate requires an HTTP MCP request") from exc

    def _request(self):
        try:
            from fastmcp.server.dependencies import get_http_request

            request = get_http_request()
        except Exception:
            raise WorkspaceError("isolate requires an HTTP MCP request")
        return request

    def _from_ip(self) -> str:
        request = self._request()
        client = getattr(request, "client", None)
        host = getattr(client, "host", None)
        if not isinstance(host, str) or not host:
            raise WorkspaceError("could not determine the client IP address")
        return self._opaque_scope_id(host)

    def _from_session(self) -> str:
        self._request()
        context = self._context()
        try:
            session_id = context.session_id
        except Exception as exc:
            raise WorkspaceError("could not determine the MCP session") from exc
        if not isinstance(session_id, str) or not session_id:
            raise WorkspaceError("could not determine the MCP session")
        return self._opaque_scope_id(session_id)
