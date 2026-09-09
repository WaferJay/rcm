"""Compatibility tests for remote runtime-section parsing."""

from __future__ import annotations

import pytest

import rcm.proxy as proxy_module
from rcm.config import ProxyTargetSpec, RemoteConfigSpec, SSHSpec
from rcm.proxy import ProxyError, _read_remote_metadata


def remote_target() -> ProxyTargetSpec:
    return ProxyTargetSpec(
        name="remote",
        transport="remote",
        ssh=SSHSpec(host="compile-machine"),
        remote_config=RemoteConfigSpec(path="/etc/rcm/commands.yaml"),
    )


@pytest.mark.parametrize(
    "document, expected",
    [
        (
            "- not\n- a\n- mapping\n",
            "remote config '/etc/rcm/commands.yaml' must contain a mapping",
        ),
        (
            "server: {transport: websocket}\n",
            "remote config '/etc/rcm/commands.yaml': server.transport must be "
            "`http` or `stdio`, got 'websocket'",
        ),
        (
            "server: {public_base_url: 'https://example.com'}\nauth: [invalid]\n",
            "remote config '/etc/rcm/commands.yaml': auth must be a mapping",
        ),
        (
            "server: {public_base_url: 'https://example.com'}\ndefaults: [invalid]\n",
            "remote config '/etc/rcm/commands.yaml': defaults must be a mapping",
        ),
    ],
)
@pytest.mark.asyncio
async def test_remote_runtime_errors_keep_their_context(
    document: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ssh(_: str, command: str) -> tuple[int, str, str]:
        assert command == "cat -- /etc/rcm/commands.yaml"
        return 0, document, ""

    monkeypatch.setattr(proxy_module, "_ssh_command", fake_ssh)

    with pytest.raises(ProxyError) as exc_info:
        await _read_remote_metadata(remote_target())
    assert str(exc_info.value) == expected


@pytest.mark.asyncio
async def test_remote_invalid_yaml_keeps_the_existing_error_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ssh(_: str, command: str) -> tuple[int, str, str]:
        return 0, "server: [", ""

    monkeypatch.setattr(proxy_module, "_ssh_command", fake_ssh)

    with pytest.raises(
        ProxyError,
        match=r"^invalid YAML in remote config '/etc/rcm/commands.yaml':",
    ):
        await _read_remote_metadata(remote_target())
