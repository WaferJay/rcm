"""Internal protocol for moving rcm command output across stdio proxies."""

from __future__ import annotations

ARTIFACT_ENV = "RCM_MCP_PROXY_ARTIFACTS"
ARTIFACT_ENV_VALUE = "inline-base64"
ARTIFACT_PROTOCOL = "rcm-inline-base64-v1"
