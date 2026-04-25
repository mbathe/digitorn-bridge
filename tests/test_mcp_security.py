"""Tests — MCP Security: environment sanitization, server config validation.

Covers:
- build_safe_env() inherits only safe vars
- build_safe_env() adds explicit vars from YAML
- build_safe_env() blocks dangerous vars (defense in depth)
- validate_server_config() validates transport types
- validate_server_config() validates required fields per transport
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from digitorn.modules.mcp.security import (
    _BLOCKED_ENV_KEYS,
    _SAFE_ENV_KEYS,
    build_safe_env,
    validate_server_config,
)


# ── build_safe_env ───────────────────────────────────────────────────────


class TestBuildSafeEnv:
    def test_inherits_safe_vars_only(self):
        fake_env = {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "SECRET_KEY": "should_not_appear",
            "API_KEY": "should_not_appear",
        }
        with patch.dict("os.environ", fake_env, clear=True):
            env = build_safe_env({})

        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/test"
        assert "SECRET_KEY" not in env
        assert "API_KEY" not in env

    def test_adds_explicit_vars(self):
        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            env = build_safe_env({"SLACK_TOKEN": "xoxb-test", "MY_VAR": "hello"})

        assert env["SLACK_TOKEN"] == "xoxb-test"
        assert env["MY_VAR"] == "hello"

    def test_explicit_overrides_safe(self):
        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            env = build_safe_env({"PATH": "/custom/path"})

        assert env["PATH"] == "/custom/path"

    def test_blocks_dangerous_vars(self):
        blocked = {key: "secret_value" for key in _BLOCKED_ENV_KEYS}
        with patch.dict("os.environ", {}, clear=True):
            env = build_safe_env(blocked)

        for key in _BLOCKED_ENV_KEYS:
            assert key not in env, f"{key} should be blocked"

    def test_blocked_does_not_affect_other_vars(self):
        with patch.dict("os.environ", {}, clear=True):
            env = build_safe_env({
                "DATABASE_URL": "should_block",
                "GOOD_VAR": "should_keep",
            })

        assert "DATABASE_URL" not in env
        assert env["GOOD_VAR"] == "should_keep"

    def test_node_and_python_paths_not_inherited(self):
        """NODE_PATH and PYTHONPATH are excluded to prevent library injection."""
        with patch.dict("os.environ", {
            "NODE_PATH": "/node",
            "PYTHONPATH": "/py",
            "NODE_ENV": "production",
        }, clear=True):
            env = build_safe_env({})

        assert "NODE_PATH" not in env
        assert "PYTHONPATH" not in env
        assert env["NODE_ENV"] == "production"

    def test_node_and_python_paths_allowed_via_explicit_env(self):
        """MCP servers can still get PYTHONPATH/NODE_PATH if declared in YAML."""
        with patch.dict("os.environ", {}, clear=True):
            env = build_safe_env({"PYTHONPATH": "/custom", "NODE_PATH": "/custom-node"})

        assert env["PYTHONPATH"] == "/custom"
        assert env["NODE_PATH"] == "/custom-node"

    def test_xdg_dirs_inherited(self):
        with patch.dict("os.environ", {
            "XDG_DATA_HOME": "/data",
            "XDG_CONFIG_HOME": "/config",
            "XDG_CACHE_HOME": "/cache",
            "XDG_RUNTIME_DIR": "/run",
        }, clear=True):
            env = build_safe_env({})

        assert env["XDG_DATA_HOME"] == "/data"
        assert env["XDG_RUNTIME_DIR"] == "/run"


# ── validate_server_config ───────────────────────────────────────────────


class TestValidateServerConfig:
    def test_valid_stdio(self):
        errors = validate_server_config({
            "transport": "stdio",
            "command": "npx",
            "args": ["@test/server"],
        })
        assert errors == []

    def test_valid_sse(self):
        errors = validate_server_config({
            "transport": "sse",
            "url": "http://localhost:3000/sse",
        })
        assert errors == []

    def test_valid_http(self):
        errors = validate_server_config({
            "transport": "http",
            "url": "http://localhost:3000",
        })
        assert errors == []

    def test_valid_streamable_http(self):
        errors = validate_server_config({
            "transport": "streamable_http",
            "url": "http://localhost:3000",
        })
        assert errors == []

    def test_default_transport_is_stdio(self):
        # No transport specified → defaults to stdio → needs command
        errors = validate_server_config({"command": "echo"})
        assert errors == []

    def test_unknown_transport(self):
        errors = validate_server_config({"transport": "websocket"})
        assert len(errors) == 1
        assert "Unknown transport" in errors[0]

    def test_stdio_missing_command(self):
        errors = validate_server_config({"transport": "stdio"})
        assert len(errors) == 1
        assert "command" in errors[0].lower()

    def test_sse_missing_url(self):
        errors = validate_server_config({"transport": "sse"})
        assert len(errors) == 1
        assert "url" in errors[0].lower()

    def test_http_missing_url(self):
        errors = validate_server_config({"transport": "streamable_http"})
        assert len(errors) == 1
        assert "url" in errors[0].lower()

    def test_stdio_no_transport_no_command(self):
        errors = validate_server_config({})
        assert len(errors) == 1
        assert "command" in errors[0].lower()
