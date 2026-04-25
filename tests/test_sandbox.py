"""Sandbox tests — profile building, IPC protocol, and OS backend probing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from digitorn.core.sandbox.profile import SandboxProfile
from digitorn.core.sandbox.guard import SandboxGuard
from digitorn.core.sandbox.builder import build_sandbox_profile
from digitorn.core.sandbox.noop import NoopSandbox
from digitorn.core.sandbox.ipc import (
    IPCRequest,
    IPCResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)


# ── Helpers ───────────────────────────────────────────────────────


def _mock_compiled(
    *,
    app_id: str = "test-app",
    workspace: str = "/home/user/workspace",
    modules: dict[str, Any] | None = None,
    security: bool = True,
    granted_permissions: frozenset[str] | None = None,
) -> Any:
    mods = {}
    for mid, cfg in (modules or {}).items():
        mods[mid] = SimpleNamespace(
            constraints=cfg.get("constraints", {}),
            config=cfg.get("config", {}),
        )

    sec = None
    if security:
        sec = SimpleNamespace(
            can_access_module=lambda mid: True,
            granted_permissions=granted_permissions or frozenset(),
        )

    compiled = SimpleNamespace(
        meta=SimpleNamespace(app_id=app_id),
        execution=SimpleNamespace(workspace=workspace, resources=None),
        modules=mods,
        module_ids=list(mods.keys()),
        security_profile=sec,
        source_path=None,
    )
    return compiled


# ═══════════════════════════════════════════════════════════════════
# Profile building
# ═══════════════════════════════════════════════════════════════════


class TestSandboxProfile:

    def test_system_paths_added(self):
        import platform as _plat
        prof = SandboxProfile(app_id="x")
        prof.add_system_paths()
        # /tmp is NOT globally writable (per-session private tmpdir instead)
        assert "/tmp" not in prof.writable_paths
        # ~/.digitorn is read-only (secrets), per-app state dir is writable
        home = str(Path.home() / ".digitorn")
        assert home in prof.readable_paths
        app_state = str(Path.home() / ".digitorn" / "app_state" / "x")
        assert app_state in prof.writable_paths
        # Platform-specific system paths
        _os = _plat.system().lower()
        if _os in ("linux", "darwin"):
            assert "/usr" in prof.readable_paths
        elif _os == "windows":
            import os
            win_root = os.environ.get("SystemRoot", r"C:\Windows")
            assert win_root in prof.readable_paths
        # Python runtime paths are auto-detected from sys.path
        import sys
        assert any(sp in prof.readable_paths for sp in sys.path if sp)

    def test_has_fs_restrictions(self):
        p = SandboxProfile(app_id="x")
        assert p.has_fs_restrictions is True
        p.fs_unrestricted = True
        assert p.has_fs_restrictions is False

    def test_all_allowed_paths(self):
        p = SandboxProfile(app_id="x")
        p.writable_paths.add("/a")
        p.readable_paths.add("/b")
        assert p.all_allowed_paths == {"/a", "/b"}


# ═══════════════════════════════════════════════════════════════════
# Builder: YAML → SandboxProfile
# ═══════════════════════════════════════════════════════════════════


class TestSandboxBuilder:

    def test_no_security_profile_unrestricted(self):
        compiled = _mock_compiled(security=False)
        profile = build_sandbox_profile(compiled)
        assert profile.fs_unrestricted is True
        assert profile.allow_exec is True
        assert profile.allow_network is True

    def test_filesystem_paths_constraint(self):
        compiled = _mock_compiled(modules={
            "filesystem": {"constraints": {"paths": ["/data", "/workspace"]}},
        })
        profile = build_sandbox_profile(compiled)
        resolved = {str(Path(p).resolve()) for p in ["/data", "/workspace"]}
        assert resolved.issubset(profile.writable_paths)
        assert profile.fs_unrestricted is False

    def test_filesystem_unrestricted(self):
        compiled = _mock_compiled(modules={
            "filesystem": {"constraints": {"unrestricted": True}},
        })
        profile = build_sandbox_profile(compiled)
        assert profile.fs_unrestricted is True

    def test_filesystem_no_paths_defaults_to_workspace(self):
        compiled = _mock_compiled(
            workspace="/my/workspace",
            modules={"filesystem": {"constraints": {}}},
        )
        profile = build_sandbox_profile(compiled)
        assert str(Path("/my/workspace").resolve()) in profile.writable_paths

    def test_shell_enables_exec(self):
        compiled = _mock_compiled(modules={
            "shell": {"constraints": {"allowed_paths": ["/opt/tools"]}},
        })
        profile = build_sandbox_profile(compiled)
        assert profile.allow_exec is True
        assert profile.allow_fork is True
        assert str(Path("/opt/tools").resolve()) in profile.readable_paths

    def test_web_enables_network(self):
        compiled = _mock_compiled(modules={
            "web": {"constraints": {"allowed_domains": ["github.com", "pypi.org"]}},
        })
        profile = build_sandbox_profile(compiled)
        assert profile.allow_network is True
        assert "github.com" in profile.allowed_hosts
        assert "pypi.org" in profile.allowed_hosts

    def test_http_enables_network(self):
        compiled = _mock_compiled(modules={
            "http": {"constraints": {"allowed_hosts": ["api.example.com"]}},
        })
        profile = build_sandbox_profile(compiled)
        assert profile.allow_network is True
        assert "api.example.com" in profile.allowed_hosts

    def test_database_allows_localhost(self):
        compiled = _mock_compiled(modules={
            "database": {"constraints": {"allowed_hosts": ["db.prod.com"]}},
        })
        profile = build_sandbox_profile(compiled)
        assert "localhost" in profile.allowed_hosts
        assert "db.prod.com" in profile.allowed_hosts

    def test_git_enables_exec_and_workspace_write(self):
        compiled = _mock_compiled(
            workspace="/project",
            modules={"git": {"constraints": {}}},
        )
        profile = build_sandbox_profile(compiled)
        assert profile.allow_exec is True
        assert str(Path("/project").resolve()) in profile.writable_paths

    def test_mcp_with_sandbox_block(self):
        """MCP server with sandbox: block gets only declared permissions."""
        compiled = _mock_compiled(modules={
            "mcp": {
                "constraints": {},
                "config": {
                    "servers": {
                        "github": {
                            "command": "npx @mcp/github",
                            "sandbox": {
                                "permissions": ["process.exec", "net.http", "fs.read"],
                                "paths": {"read": ["/data"]},
                                "allowed_hosts": ["api.github.com"],
                            }
                        }
                    }
                },
            },
        })
        profile = build_sandbox_profile(compiled)
        assert profile.allow_exec is True
        assert profile.allow_fork is True
        assert profile.allow_network is True
        assert "api.github.com" in profile.allowed_hosts
        assert str(Path("/data").resolve()) in profile.readable_paths

    def test_mcp_without_sandbox_blocked(self):
        """MCP server without sandbox: block gets NO OS-level rights (deny-by-default)."""
        compiled = _mock_compiled(modules={
            "mcp": {
                "constraints": {},
                "config": {"servers": {"tool": {}}},
            },
        })
        profile = build_sandbox_profile(compiled)
        assert profile.allow_exec is False
        assert profile.allow_fork is False
        assert profile.allow_network is False

    def test_mcp_partial_permissions(self):
        """MCP server declaring only net.http gets network but not exec."""
        compiled = _mock_compiled(modules={
            "mcp": {
                "constraints": {},
                "config": {
                    "servers": {
                        "remote": {
                            "url": "https://mcp.example.com",
                            "sandbox": {
                                "permissions": ["net.http"],
                                "allowed_hosts": ["mcp.example.com"],
                            }
                        }
                    }
                },
            },
        })
        profile = build_sandbox_profile(compiled)
        assert profile.allow_exec is False
        assert profile.allow_network is True
        assert "mcp.example.com" in profile.allowed_hosts

    def test_unknown_module_with_sandbox_block(self):
        compiled = _mock_compiled(modules={
            "custom_ai": {
                "constraints": {},
                "config": {
                    "sandbox": {
                        "permissions": ["net.http", "fs.write"],
                        "paths": {"write": ["/output"]},
                    }
                },
            },
        })
        profile = build_sandbox_profile(compiled)
        assert profile.allow_network is True
        assert str(Path("/output").resolve()) in profile.writable_paths

    def test_granted_permissions_infer_flags(self):
        compiled = _mock_compiled(
            modules={},
            granted_permissions=frozenset(["process.*", "net.http"]),
        )
        profile = build_sandbox_profile(compiled)
        assert profile.allow_exec is True
        assert profile.allow_network is True

    def test_no_modules_minimal_profile(self):
        compiled = _mock_compiled(modules={})
        profile = build_sandbox_profile(compiled)
        assert profile.allow_exec is False
        assert profile.allow_network is False
        assert profile.fs_unrestricted is False

    def test_combined_modules(self):
        compiled = _mock_compiled(
            workspace="/project",
            modules={
                "filesystem": {"constraints": {"paths": ["/project", "/shared"]}},
                "shell": {"constraints": {}},
                "web": {"constraints": {"allowed_domains": ["docs.python.org"]}},
                "database": {"constraints": {"allowed_hosts": ["db.local"]}},
            },
        )
        profile = build_sandbox_profile(compiled)
        assert profile.allow_exec is True
        assert profile.allow_network is True
        assert str(Path("/project").resolve()) in profile.writable_paths
        assert str(Path("/shared").resolve()) in profile.writable_paths
        assert "docs.python.org" in profile.allowed_hosts
        assert "db.local" in profile.allowed_hosts


# ═══════════════════════════════════════════════════════════════════
# IPC protocol
# ═══════════════════════════════════════════════════════════════════


class TestIPC:

    def test_request_roundtrip(self):
        req = IPCRequest(id=42, method="chat", payload={"message": "hi"})
        encoded = encode_request(req)
        decoded = decode_request(encoded)
        assert decoded.id == 42
        assert decoded.method == "chat"
        assert decoded.payload["message"] == "hi"

    def test_response_roundtrip(self):
        resp = IPCResponse(id=7, success=True, data={"content": "done"})
        encoded = encode_response(resp)
        decoded = decode_response(encoded)
        assert decoded.id == 7
        assert decoded.success is True
        assert decoded.data["content"] == "done"

    def test_error_response(self):
        resp = IPCResponse(id=3, success=False, error="boom")
        encoded = encode_response(resp)
        decoded = decode_response(encoded)
        assert decoded.success is False
        assert decoded.error == "boom"

    def test_event_response(self):
        resp = IPCResponse(id=5, event="token", data={"text": "Hello"})
        encoded = encode_response(resp)
        decoded = decode_response(encoded)
        assert decoded.event == "token"
        assert decoded.data["text"] == "Hello"

    def test_unicode(self):
        req = IPCRequest(id=1, method="chat", payload={"message": "héllo 世界"})
        encoded = encode_request(req)
        decoded = decode_request(encoded)
        assert decoded.payload["message"] == "héllo 世界"


# ═══════════════════════════════════════════════════════════════════
# Guard
# ═══════════════════════════════════════════════════════════════════


class TestSandboxGuard:

    def test_enforced(self):
        g = SandboxGuard(app_id="x", active=["landlock_v7", "seccomp"])
        assert g.is_enforced is True
        assert g.is_partial is False

    def test_partial(self):
        g = SandboxGuard(app_id="x", active=["seccomp"], unavailable=["landlock"])
        assert g.is_enforced is True
        assert g.is_partial is True

    def test_noop(self):
        g = SandboxGuard(app_id="x")
        assert g.is_enforced is False


# ═══════════════════════════════════════════════════════════════════
# Backend probing
# ═══════════════════════════════════════════════════════════════════


class TestBackendProbe:

    def test_noop_probe(self):
        backend = NoopSandbox()
        assert backend.probe() == []

    def test_noop_apply(self):
        backend = NoopSandbox()
        guard = backend.apply(SandboxProfile(app_id="test"))
        assert not guard.is_enforced
        assert "all" in guard.unavailable

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
    def test_linux_probe(self):
        from digitorn.core.sandbox.linux import LinuxSandbox
        backend = LinuxSandbox()
        features = backend.probe()
        assert isinstance(features, list)
        assert any("landlock" in f or "seccomp" in f for f in features)

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
    def test_landlock_abi_detection(self):
        from digitorn.core.sandbox.landlock import detect_abi_version
        abi = detect_abi_version()
        assert abi >= 0  # 0 if kernel too old, >0 if available

    def test_get_backend_returns_something(self):
        from digitorn.core.sandbox import get_backend
        backend = get_backend()
        assert hasattr(backend, "name")
        assert hasattr(backend, "probe")
        assert hasattr(backend, "apply")
