"""YAML decoding and filesystem loading for :mod:`rcm.config`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import Config, ConfigError
from .parsing import parse_config_document


def decode_yaml_mapping(
    text: str,
    source: str,
    *,
    mapping_error: str | None = None,
) -> dict[str, Any]:
    """Decode one YAML document and require a top-level mapping."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(
            mapping_error or f"top-level YAML must be a mapping in {source}"
        )
    return raw


def load_config(path: str | Path) -> Config:
    """Read, decode, and validate an RCM configuration file."""
    resolved = Path(path).expanduser().resolve()
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {resolved}")
    raw = decode_yaml_mapping(text, str(resolved))
    return parse_config_document(raw, resolved)
