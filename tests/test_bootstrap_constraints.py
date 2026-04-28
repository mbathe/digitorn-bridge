"""Guard tests: constraints injection and SSRF policy.

These tests ensure that:
1. Module constraints are always injected before on_start()
2. SSRF protection blocks private IPs by default
3. allowed_hosts bypasses SSRF only for admin-declared hosts
4. No module can bypass SSRF without explicit YAML declaration
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest


# ── Constraints Injection ───────────────────────────────────────────


class TestConstraintsInjection:
    """Verify that bootstrap injects constraints into ALL modules."""

    @pytest.mark.asyncio
    async def test_http_constraints_injected(self):
        """HTTP module receives allowed_hosts from YAML constraints."""
        compiled, boot = await _compile_and_boot(
            modules_yaml='http:\n    constraints:\n      allowed_hosts: ["example.com"]',
        )
        http_mod = boot["modules"].get("http")
        assert http_mod is not None
        assert http_mod._allowed_hosts == ["example.com"]

    @pytest.mark.asyncio
    async def test_filesystem_constraints_injected(self):
        """Filesystem module receives paths from YAML constraints."""
        compiled, boot = await _compile_and_boot(
            modules_yaml='filesystem:\n    constraints:\n      paths: ["/tmp/test-guard"]',
        )
        fs_mod = boot["modules"].get("filesystem")
        assert fs_mod is not None
        constraints = getattr(fs_mod, "_constraints", {})
        assert "/tmp/test-guard" in constraints.get("paths", [])

    @pytest.mark.asyncio
    async def test_empty_constraints_no_crash(self):
        """Modules without constraints don't crash during bootstrap."""
        compiled, boot = await _compile_and_boot(
            modules_yaml="http: {}",
        )
        http_mod = boot["modules"].get("http")
        assert http_mod is not None
        assert http_mod._allowed_hosts == []


# ── SSRF Protection ────────────────────────────────────────────────


class TestSSRFProtection:
    """Verify SSRF protection is correct and cannot be bypassed."""

    def test_localhost_blocked_by_default(self):
        """localhost is blocked when no allowed_hosts declared."""
        from digitorn.modules.http.security import validate_url

        err, _, _ = validate_url("http://localhost:9080/api")
        assert err is not None
        assert "private" in err.lower() or "blocked" in err.lower()

    def test_private_ip_blocked_by_default(self):
        """Private IPs (10.x, 172.16.x, 192.168.x) are blocked."""
        from digitorn.modules.http.security import validate_url

        for ip in ["10.0.0.1", "172.16.0.1", "192.168.1.1"]:
            err, _, _ = validate_url(f"http://{ip}:8080/api")
            assert err is not None, f"{ip} should be blocked"

    def test_loopback_blocked_by_default(self):
        """127.0.0.1 is blocked without whitelist."""
        from digitorn.modules.http.security import validate_url

        err, _, _ = validate_url("http://127.0.0.1:9080/api")
        assert err is not None

    def test_public_ip_allowed_by_default(self):
        """Public IPs pass without any allowed_hosts."""
        from digitorn.modules.http.security import validate_url

        err, _, validated = validate_url("https://api.example.com/test")
        # May fail on DNS, but should NOT fail on SSRF
        if err:
            assert "private" not in err.lower()

    def test_allowed_hosts_permits_localhost(self):
        """Explicit allowed_hosts lets localhost through."""
        from digitorn.modules.http.security import validate_url

        err, _, validated = validate_url(
            "http://localhost:9080/api",
            allowed_hosts=["localhost"],
        )
        assert err is None
        assert validated is not None

    def test_allowed_hosts_permits_private_ip(self):
        """Explicit allowed_hosts lets private IPs through."""
        from digitorn.modules.http.security import validate_url

        err, _, validated = validate_url(
            "http://127.0.0.1:9080/api",
            allowed_hosts=["127.0.0.1"],
        )
        assert err is None

    def test_allowed_hosts_does_not_open_everything(self):
        """Whitelisting one host doesn't open other private hosts."""
        from digitorn.modules.http.security import validate_url

        err, _, _ = validate_url(
            "http://192.168.1.1/admin",
            allowed_hosts=["localhost"],
        )
        assert err is not None, "192.168.1.1 should still be blocked"

    def test_blocked_hosts_overrides_allowed(self):
        """blocked_hosts takes priority over allowed_hosts."""
        from digitorn.modules.http.security import validate_url

        err, _, _ = validate_url(
            "http://evil.com/steal",
            allowed_hosts=["evil.com"],
            blocked_hosts=["evil.com"],
        )
        assert err is not None

    def test_no_bypass_without_yaml_declaration(self):
        """Empty allowed_hosts means NO private IP bypass - ever."""
        from digitorn.modules.http.security import validate_url

        for host in ["localhost", "127.0.0.1", "10.0.0.1", "192.168.0.1"]:
            err, _, _ = validate_url(
                f"http://{host}:8080/",
                allowed_hosts=[],  # Empty = no bypass
            )
            assert err is not None, f"{host} must be blocked with empty allowed_hosts"

    def test_none_allowed_hosts_means_no_bypass(self):
        """None allowed_hosts (default) means NO private IP bypass."""
        from digitorn.modules.http.security import validate_url

        err, _, _ = validate_url(
            "http://localhost:8080/",
            allowed_hosts=None,
        )
        assert err is not None


# ── Helper ──────────────────────────────────────────────────────────


async def _compile_and_boot(modules_yaml: str) -> tuple:
    """Compile a minimal YAML and bootstrap it. Returns (compiled, boot_result)."""
    import tempfile
    import yaml

    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.loader import load_modules
    from digitorn.core.runtime.bootstrap import bootstrap
    from digitorn.modules.registry import ModuleRegistry

    app_yaml = f"""
app:
  app_id: test-guard
  name: Guard Test

modules:
  {modules_yaml}

agents:
  - id: main
    brain:
      provider: test
      model: test
      backend: openai_compat
      config:
        api_key: "test"
        base_url: "http://localhost:1234"
    role: test

execution:
  mode: one_shot
"""

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)
    compiler = AppYAMLCompiler(registry)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(app_yaml)
        f.flush()
        compiled = compiler.compile_file(Path(f.name))

    boot = await bootstrap(compiled, registry)
    os.unlink(f.name)
    return compiled, boot
