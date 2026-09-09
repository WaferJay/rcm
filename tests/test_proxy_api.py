"""Compatibility tests for the public :mod:`rcm.proxy` facade."""

from __future__ import annotations

import pickle

import rcm.proxy as proxy_module
from rcm.proxy import ProxyError, ProxyRuntime, ProxyTool


def test_public_proxy_imports_remain_available() -> None:
    assert proxy_module.ProxyError is ProxyError
    assert proxy_module.ProxyRuntime is ProxyRuntime
    assert proxy_module.ProxyTool is ProxyTool
    assert ProxyError.__module__ == "rcm.proxy"
    assert ProxyRuntime.__module__ == "rcm.proxy"
    assert ProxyTool.__module__ == "rcm.proxy"
    assert proxy_module.__all__ == ["ProxyError", "ProxyRuntime", "ProxyTool"]


def test_public_proxy_pickle_paths_remain_compatible() -> None:
    for public_type in (ProxyError, ProxyRuntime, ProxyTool):
        assert pickle.loads(pickle.dumps(public_type)) is public_type

    restored = pickle.loads(pickle.dumps(ProxyError("boom")))
    assert type(restored) is ProxyError
    assert str(restored) == "boom"
