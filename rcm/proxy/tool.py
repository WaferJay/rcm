"""FastMCP tool wrapper for forwarding proxied calls."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import Tool, ToolResult
from mcp.types import TextContent
from pydantic import PrivateAttr

from ..artifacts import (
    ARTIFACT_KINDS,
    DEFAULT_ARTIFACT_MODE,
    RCM_CALL_META,
    RCM_RESULT_SCHEMA,
    ArtifactDescriptor,
    ArtifactError,
    RunResult,
    parse_run_result,
    public_run_result,
)
from ..store import Store, StoreError
from ..sync import SyncError, SyncRunner
from .artifact_transfer import ArtifactFetcher, _standard_artifact_fetcher


class ProxyTool(Tool):
    """A FastMCP tool that syncs and forwards one remote MCP tool call."""

    _target_name: str = PrivateAttr()
    _remote_name: str = PrivateAttr()
    _client: Client = PrivateAttr()
    _sync_runner: SyncRunner | None = PrivateAttr()
    _store: Store | None = PrivateAttr()
    _target_transport: str = PrivateAttr()
    _ssh_host: str | None = PrivateAttr()
    _artifact_mode: str = PrivateAttr()
    _rcm_peer: bool = PrivateAttr()
    _artifact_fetcher: ArtifactFetcher = PrivateAttr()
    _failure_reporter: Callable[[Exception], None] | None = PrivateAttr()

    def __init__(
        self,
        *,
        public_name: str,
        target_name: str,
        remote_name: str,
        description: str | None,
        parameters: dict[str, Any],
        output_schema: dict[str, Any] | None,
        client: Client,
        sync: SyncRunner | None,
        store: Store | None = None,
        target_transport: str = "stdio",
        ssh_host: str | None = None,
        artifact_mode: str = DEFAULT_ARTIFACT_MODE,
        rcm_peer: bool = False,
        artifact_fetcher: ArtifactFetcher | None = None,
        failure_reporter: Callable[[Exception], None] | None = None,
    ) -> None:
        super().__init__(
            name=public_name,
            description=description or f"Proxy for {target_name}::{remote_name}",
            parameters=parameters,
            output_schema=output_schema,
        )
        self._target_name = target_name
        self._remote_name = remote_name
        self._client = client
        self._sync_runner = sync
        self._store = store
        self._target_transport = target_transport
        self._ssh_host = ssh_host
        self._artifact_mode = artifact_mode
        self._rcm_peer = rcm_peer
        self._artifact_fetcher = artifact_fetcher or _standard_artifact_fetcher(
            target_transport,
            ssh_host,
        )
        self._failure_reporter = failure_reporter

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        if self._sync_runner is not None:
            try:
                await self._sync_runner.sync()
            except SyncError as exc:
                raise ToolError(str(exc)) from exc

        try:
            call_kwargs: dict[str, Any] = {}
            if self._rcm_peer:
                call_kwargs["meta"] = RCM_CALL_META
            result = await self._client.call_tool_mcp(
                self._remote_name, arguments or {}, **call_kwargs
            )
        except Exception as exc:
            if self._failure_reporter is not None:
                self._failure_reporter(exc)
            raise ToolError(
                f"proxy target {self._target_name!r} tool "
                f"{self._remote_name!r} failed: {exc}"
            ) from exc

        structured_content = getattr(result, "structured_content", None)
        if structured_content is None:
            structured_content = getattr(result, "structuredContent", None)
        rewritten = False
        if (
            self._rcm_peer
            and isinstance(structured_content, dict)
            and structured_content.get("schema") == RCM_RESULT_SCHEMA
        ):
            try:
                remote = parse_run_result(structured_content)
                if self._artifact_mode == "passthrough":
                    self._validate_passthrough(remote)
                else:
                    structured_content = await self._materialize_artifact(remote)
                rewritten = True
            except (ArtifactError, OSError, StoreError) as exc:
                raise ToolError(str(exc)) from exc

        return ToolResult(
            content=(
                [
                    TextContent(
                        type="text",
                        text=json.dumps(structured_content, ensure_ascii=False),
                    )
                ]
                if rewritten
                else result.content
            ),
            structured_content=structured_content,
            meta=getattr(result, "meta", None),
            is_error=getattr(result, "is_error", getattr(result, "isError", False)),
        )

    def _validate_passthrough(self, remote: RunResult) -> None:
        if self._target_transport != "http":
            raise ArtifactError("artifact passthrough requires an HTTP RCM target")
        for descriptor in remote.artifacts.values():
            try:
                parsed = urlsplit(descriptor.uri)
            except ValueError as exc:
                raise ArtifactError("artifact passthrough URL is invalid") from exc
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                raise ArtifactError(
                    "artifact passthrough requires absolute HTTP artifact URLs"
                )
            if parsed.username is not None or parsed.password is not None:
                raise ArtifactError(
                    "artifact passthrough URLs cannot contain user information"
                )

    async def _copy_artifact(
        self,
        artifact: str,
        run_id: str,
        descriptor: ArtifactDescriptor,
        destination: Path,
    ) -> None:
        await self._artifact_fetcher.copy(
            artifact,
            run_id,
            descriptor,
            destination,
        )

    async def _materialize_artifact(self, remote: RunResult) -> dict[str, Any]:
        if self._store is None:
            raise ArtifactError("rcm artifact received without a local store")
        run_id, staging = self._store.create_staging_run()
        try:
            for name, descriptor in remote.artifacts.items():
                await self._copy_artifact(
                    name,
                    remote.run_id,
                    descriptor,
                    staging / ARTIFACT_KINDS[name].filename,
                )
            artifacts = {
                name: ArtifactDescriptor(
                    uri=self._store.url_for(run_id, name),
                    bytes=descriptor.bytes,
                    sha256=descriptor.sha256,
                )
                for name, descriptor in remote.artifacts.items()
            }
            local = public_run_result(
                run_id=run_id,
                returncode=remote.returncode,
                timed_out=remote.timed_out,
                duration_ms=remote.duration_ms,
                artifacts=artifacts,
                warnings=remote.warnings,
                extra_fields=remote.extra_fields,
            )
            meta = {
                **local,
                "upstream": {
                    "target": self._target_name,
                    "run_id": remote.run_id,
                    "transport": self._target_transport,
                },
            }
            self._store.commit_staging_run(run_id, staging, meta)
            return local
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
