"""MCP security - environment variable sanitization and subprocess sandboxing."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL",
    "TMPDIR", "TMP", "TEMP",
    # NODE_PATH and PYTHONPATH intentionally excluded - they allow
    # library injection attacks on MCP subprocesses.  If an MCP server
    # needs them, declare them explicitly in the YAML env: block.
    "NODE_ENV",
    "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
})

_BLOCKED_ENV_KEYS = frozenset({
    "DIGITORN_DB_URL", "DIGITORN_SECRET_KEY",
    "DATABASE_URL", "DB_PASSWORD",
    "AWS_SECRET_ACCESS_KEY",
    "PRIVATE_KEY", "SSL_KEY",
})

def build_safe_env(explicit_env: dict[str, str]) -> dict[str, str]:
    """Build a safe environment for an MCP subprocess."""
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}

    for key, value in explicit_env.items():
        if key in _BLOCKED_ENV_KEYS:
            logger.warning("mcp_env_blocked key=%s", key)
            continue
        env[key] = value

    return env

def validate_server_config(server_config: dict[str, Any]) -> list[str]:
    """Validate an MCP server configuration."""
    errors: list[str] = []

    transport = server_config.get("transport", "stdio")
    if transport not in ("stdio", "sse", "streamable_http", "http"):
        errors.append(f"Unknown transport: {transport}")

    if transport == "stdio":
        command = server_config.get("command")
        if not command:
            errors.append("stdio transport requires 'command'")
    elif transport in ("sse", "streamable_http", "http"):
        url = server_config.get("url")
        if not url:
            errors.append(f"{transport} transport requires 'url'")

    return errors
