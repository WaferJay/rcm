"""Reusable scalar and path validators for configuration parsing."""

from __future__ import annotations

from typing import Any

from .models import ConfigError


def parse_non_empty_argv(raw: Any, context: str) -> list[str] | None:
    """Parse an optional argv list while retaining the existing error contract."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{context} must be a non-empty list of strings")
    if any(not isinstance(part, str) or not part for part in raw):
        raise ConfigError(f"{context} must contain only non-empty strings")
    return raw


def parse_optional_non_empty_string(raw: Any, context: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{context} must be a non-empty string")
    return raw.strip()


def validate_proxy_glob(pattern: Any, index: int, context: str) -> str:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ConfigError(f"{context}.excludes[{index}] must be a non-empty string")
    pattern = pattern.strip()
    if pattern.startswith("/") or "\\" in pattern:
        raise ConfigError(f"{context}.excludes[{index}] must be a relative POSIX glob")

    parts = pattern.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ConfigError(
            f"{context}.excludes[{index}] must not contain empty, `.` or `..` "
            "path parts"
        )
    for part in parts:
        bracket_open = False
        for char in part:
            if char == "[":
                if bracket_open:
                    raise ConfigError(
                        f"{context}.excludes[{index}] has an invalid character class"
                    )
                bracket_open = True
            elif char == "]":
                if not bracket_open:
                    raise ConfigError(
                        f"{context}.excludes[{index}] has an invalid character class"
                    )
                bracket_open = False
        if bracket_open:
            raise ConfigError(
                f"{context}.excludes[{index}] has an invalid character class"
            )
    return pattern


def parse_sync_destination(raw: Any, context: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{context}.destination must be a non-empty string")

    destination = raw.strip()
    first_slash = destination.find("/")
    colon = destination.find(":")
    host_qualified = colon >= 0 and (first_slash < 0 or colon < first_slash)
    if host_qualified and colon == 0:
        raise ConfigError(f"{context}.destination must contain a host before `:`")
    path = destination[colon + 1 :] if host_qualified else destination
    if not path:
        raise ConfigError(f"{context}.destination must contain a path")
    if "\\" in path or "~" in path:
        raise ConfigError(
            f"{context}.destination must be an absolute or relative POSIX path "
            "without `~`"
        )

    trimmed = path.rstrip("/") or "/"
    parts = [] if trimmed == "/" else trimmed.split("/")
    if trimmed.startswith("/") and trimmed != "/":
        parts = parts[1:]
    if any(part in {"", ".", ".."} for part in parts):
        raise ConfigError(
            f"{context}.destination must not contain empty, `.` or `..` path parts"
        )
    return destination
