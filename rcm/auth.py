"""API key authentication middleware for the MCP protocol layer.

Custom HTTP routes registered with `@mcp.custom_route` are intentionally
NOT covered by this middleware — output download endpoints are public and
gated only by the unguessable run_id (capability URLs).
"""

from __future__ import annotations

import secrets

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext


class ApiKeyAuth(Middleware):
    def __init__(self, expected: str) -> None:
        if not expected:
            raise ValueError("api key must be non-empty")
        self._expected = expected

    async def on_request(self, context: MiddlewareContext, call_next):
        # get_http_headers() strips `authorization` by default; opt back in.
        headers = get_http_headers(include={"authorization"}) or {}
        token = headers.get("authorization", "")
        if not token.lower().startswith("bearer "):
            raise ToolError("unauthorized")
        presented = token[7:].strip()
        if not secrets.compare_digest(presented, self._expected):
            raise ToolError("unauthorized")
        return await call_next(context)
