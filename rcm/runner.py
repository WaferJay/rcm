"""Subprocess execution with placeholder substitution and on-disk output capture."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import string
import time
from datetime import datetime, timezone
from typing import Any

from .config import CommandSpec, ParamSpec
from .artifacts import ARTIFACT_ENV, ARTIFACT_ENV_VALUE, ARTIFACT_PROTOCOL
from .store import Store


class RunError(Exception):
    """Raised on parameter validation / substitution failure."""


def _coerce(value: Any, spec: ParamSpec) -> Any:
    """Coerce a JSON value coming from MCP into the declared param type."""
    t = spec.type
    if t == "string":
        if not isinstance(value, str):
            raise RunError(f"param {spec.name!r} must be a string")
        return value
    if t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise RunError(f"param {spec.name!r} must be an integer")
        return value
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RunError(f"param {spec.name!r} must be a number")
        return value
    if t == "boolean":
        if not isinstance(value, bool):
            raise RunError(f"param {spec.name!r} must be a boolean")
        return value
    raise RunError(f"unsupported type for {spec.name!r}: {t}")


def _validate_param(value: Any, spec: ParamSpec) -> str:
    """Coerce + validate one param value, return its string form for substitution."""
    coerced = _coerce(value, spec)
    if spec.enum is not None and coerced not in spec.enum:
        raise RunError(
            f"param {spec.name!r}: value {coerced!r} not in enum {spec.enum!r}"
        )
    s = "true" if coerced is True else "false" if coerced is False else str(coerced)
    if spec.pattern is not None and not re.fullmatch(spec.pattern, s):
        raise RunError(
            f"param {spec.name!r}: value does not match pattern {spec.pattern!r}"
        )
    return s


def _substitute(argv_template: list[str], values: dict[str, str]) -> list[str]:
    out: list[str] = []
    formatter = string.Formatter()
    for elem in argv_template:
        # Fast path: whole element is a single placeholder => preserve type-ish behaviour.
        parsed = list(formatter.parse(elem))
        if (
            len(parsed) == 1
            and parsed[0][0] == ""
            and parsed[0][1] is not None
            and parsed[0][2] in (None, "")
        ):
            name = parsed[0][1]
            out.append(values[name])
        elif any(p[1] is not None for p in parsed):
            out.append(elem.format_map(values))
        else:
            out.append(elem)
    return out


def _resolve_params(
    spec: CommandSpec, supplied: dict[str, Any]
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for p in spec.params:
        if p.name in supplied:
            resolved[p.name] = _validate_param(supplied[p.name], p)
        elif p.has_default:
            resolved[p.name] = _validate_param(p.default, p)
        else:
            raise RunError(f"missing required param {p.name!r}")
    extra = set(supplied) - {p.name for p in spec.params}
    if extra:
        raise RunError(f"unexpected params: {sorted(extra)}")
    return resolved


async def run_command(
    spec: CommandSpec,
    supplied_params: dict[str, Any],
    *,
    store: Store,
    default_timeout: float | None,
    default_cwd: str | None,
) -> dict[str, Any]:
    """Execute one configured command, capturing output to disk."""
    resolved = _resolve_params(spec, supplied_params)
    argv = _substitute(spec.command, resolved)

    timeout = spec.timeout if spec.timeout is not None else default_timeout
    cwd = spec.cwd if spec.cwd is not None else default_cwd

    run_id, run_dir = store.create_run()
    stdout_path = store.file_path(run_id, "stdout")
    stderr_path = store.file_path(run_id, "stderr")

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    timed_out = False
    returncode = -1
    spawn_failed = False

    with open(stdout_path, "wb") as out_f, open(stderr_path, "wb") as err_f:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=out_f,
                stderr=err_f,
                cwd=cwd,
            )
        except FileNotFoundError as e:
            err_f.write(f"rcm: executable not found: {e}\n".encode())
            spawn_failed = True
            proc = None

        if proc is not None:
            try:
                if timeout is not None:
                    returncode = await asyncio.wait_for(
                        proc.wait(), timeout=timeout
                    )
                else:
                    returncode = await proc.wait()
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    returncode = await proc.wait()
                except Exception:
                    returncode = -1
                err_f.write(
                    f"\nrcm: command timed out after {timeout}s; killed\n".encode()
                )

    if spawn_failed:
        returncode = 127

    duration_ms = int((time.monotonic() - t0) * 1000)
    ended_at = datetime.now(timezone.utc).isoformat()
    meta = _build_meta(
        run_id=run_id,
        spec=spec,
        params=resolved,
        argv=argv,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        returncode=returncode,
        timed_out=timed_out,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    store.write_meta(run_id, meta)
    return _public_result(meta, store)


def _build_meta(
    *,
    run_id: str,
    spec: CommandSpec,
    params: dict[str, str],
    argv: list[str],
    started_at: str,
    ended_at: str,
    duration_ms: int,
    returncode: int,
    timed_out: bool,
    stdout_path,
    stderr_path,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "command_name": spec.name,
        "argv": argv,
        "params": params,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout_bytes": _safe_size(stdout_path),
        "stderr_bytes": _safe_size(stderr_path),
    }


def _safe_size(path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _public_result(meta: dict[str, Any], store: Store) -> dict[str, Any]:
    run_id = meta["run_id"]
    result = {
        "run_id": run_id,
        "returncode": meta["returncode"],
        "timed_out": meta["timed_out"],
        "duration_ms": meta["duration_ms"],
        "stdout_bytes": meta["stdout_bytes"],
        "stderr_bytes": meta["stderr_bytes"],
        "stdout_url": store.url_for(run_id, "stdout"),
        "stderr_url": store.url_for(run_id, "stderr"),
    }
    if os.environ.get(ARTIFACT_ENV) == ARTIFACT_ENV_VALUE:
        result["artifact_protocol"] = ARTIFACT_PROTOCOL
        result["stdout_base64"] = base64.b64encode(
            store.file_path(run_id, "stdout").read_bytes()
        ).decode("ascii")
        result["stderr_base64"] = base64.b64encode(
            store.file_path(run_id, "stderr").read_bytes()
        ).decode("ascii")
        result.pop("stdout_url")
        result.pop("stderr_url")
    return result
