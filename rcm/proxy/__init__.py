"""MCP proxy target management and active-sync tool wrappers."""

from .errors import ProxyError
from .runtime import ProxyRuntime
from .tool import ProxyTool


# Preserve the stable import and introspection paths from the former module.
_PUBLIC_TYPES = (ProxyError, ProxyTool, ProxyRuntime)
for _public_type in _PUBLIC_TYPES:
    _public_type.__module__ = __name__
del _public_type, _PUBLIC_TYPES


__all__ = ["ProxyError", "ProxyRuntime", "ProxyTool"]
