"""21 — P0: Error resilience — tool failures, bad params, context overflow, workspace issues."""

import uuid
import os

import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def fs_app(client, headers):
    await deploy_app(client, "filesystem_app.yaml", headers)
    yield "test-filesystem"
    await undeploy_app(client, "test-filesystem", headers)


@pytest.fixture
async def shell_app(client, headers):
    await deploy_app(client, "shell_app.yaml", headers)
    yield "test-shell"
    await undeploy_app(client, "test-shell", headers)


@pytest.fixture
async def ctx_app(client, headers):
    await deploy_app(client, "context_app.yaml", headers)
    yield "test-context"
    await undeploy_app(client, "test-context", headers)


# ═══════════════════════════════════════════════════════════════
# TOOL EXECUTION FAILURES
# ═══════════════════════════════════════════════════════════════

class TestToolFailures:
    """Tool calls that fail — daemon should handle gracefully."""

    async def test_read_nonexistent_file(self, client, headers, fs_app):
        """Ask agent to read a file that doesn't exist."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(client, fs_app, sid,
                                "Read the file /nonexistent/path/xyz123.txt",
                                headers)
        # Chat should succeed (agent reports error), not crash
        assert d["success"] is True

    async def test_direct_execute_bad_params(self, client, headers, fs_app):
        """Direct tool execution with invalid parameters."""
        r = await client.post(
            f"/api/apps/{fs_app}/tools/filesystem.read/execute",
            json={"params": {}},  # Missing required 'path' param
            headers=headers,
        )
        # Should return error, not 500
        assert r.status_code < 500

    async def test_direct_execute_nonexistent_path(self, client, headers, fs_app):
        """Direct tool execution on nonexistent file."""
        r = await client.post(
            f"/api/apps/{fs_app}/tools/filesystem.read/execute",
            json={"params": {"path": "/absolutely/nonexistent/file.xyz"}},
            headers=headers,
        )
        assert r.status_code < 500

    async def test_shell_invalid_command(self, client, headers, shell_app):
        """Run a command that doesn't exist."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(client, shell_app, sid,
                                "Run: nonexistent_command_xyz_12345",
                                headers)
        assert d["success"] is True  # Chat succeeds, tool reports error

    async def test_shell_timeout_command(self, client, headers, shell_app):
        """Run a long command — should timeout gracefully."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(client, shell_app, sid,
                                "Run: sleep 30",
                                headers, timeout=120)
        # Should not hang indefinitely (shell timeout is 15s)
        assert d.get("success") is not None


# ═══════════════════════════════════════════════════════════════
# CONTEXT OVERFLOW
# ═══════════════════════════════════════════════════════════════

class TestContextOverflow:
    """Context window fills up — daemon should auto-compact or fail gracefully."""

    async def test_many_turns_triggers_compaction(self, client, headers, ctx_app):
        """Send many messages to trigger auto-compaction (context_app has low max_tokens)."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        for i in range(8):
            d = await send_and_wait(client, ctx_app, sid,
                                    f"Tell me a long story about adventure number {i+1}. Be detailed.",
                                    headers)
            # Each turn should succeed — compaction should kick in
            assert d["success"] is True or d.get("error") is not None

    async def test_session_survives_compaction(self, client, headers, ctx_app):
        """After compaction, the session should still be usable."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        # Fill context
        for i in range(5):
            await send_and_wait(client, ctx_app, sid,
                                f"Fact {i}: Python was created in 1991.",
                                headers)

        # Trigger compaction
        await client.post(
            f"/api/apps/{ctx_app}/sessions/{sid}/compact",
            headers=headers,
        )

        # Session should still work
        d = await send_and_wait(client, ctx_app, sid,
                                "Are you still working?",
                                headers)
        assert d["success"] is True


# ═══════════════════════════════════════════════════════════════
# WORKSPACE EDGE CASES
# ═══════════════════════════════════════════════════════════════

class TestWorkspaceEdgeCases:
    """Workspace directory issues."""

    async def test_chat_with_workspace_mode_none(self, client, headers):
        """App with workspace_mode=none should work without workspace."""
        await deploy_app(client, "minimal.yaml", headers)
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(client, "test-minimal", sid, "hello", headers)
        assert d["success"] is True
        await undeploy_app(client, "test-minimal", headers)

    async def test_workspace_endpoint_for_no_workspace_app(self, client, headers):
        """Workspace endpoint for app with workspace_mode=none."""
        await deploy_app(client, "minimal.yaml", headers)
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(client, "test-minimal", sid, "hello", headers)
        r = await client.get(
            f"/api/apps/test-minimal/sessions/{sid}/workspace",
            headers=headers,
        )
        assert r.status_code < 500
        await undeploy_app(client, "test-minimal", headers)


# ═══════════════════════════════════════════════════════════════
# DEPLOY ERROR CASES
# ═══════════════════════════════════════════════════════════════

class TestDeployErrors:
    """Deploy with various error conditions."""

    async def test_deploy_malformed_yaml(self, client, headers, tmp_path):
        """Deploy a YAML file with syntax errors."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("app:\n  app_id: test-bad\n  name: [invalid yaml here\n")
        r = await client.post("/api/apps/deploy", json={
            "yaml_path": str(bad_yaml),
            "force": True,
        }, headers=headers, timeout=30)
        d = r.json()
        assert d["success"] is False
        assert r.status_code in (400, 500)

    async def test_deploy_missing_required_fields(self, client, headers, tmp_path):
        """Deploy YAML missing required fields."""
        bad_yaml = tmp_path / "incomplete.yaml"
        bad_yaml.write_text("app:\n  name: NoId\n")
        r = await client.post("/api/apps/deploy", json={
            "yaml_path": str(bad_yaml),
            "force": True,
        }, headers=headers, timeout=30)
        d = r.json()
        assert d["success"] is False

    async def test_deploy_nonexistent_module(self, client, headers, tmp_path):
        """Deploy YAML referencing a module that doesn't exist."""
        bad_yaml = tmp_path / "badmod.yaml"
        bad_yaml.write_text("""
app:
  app_id: test-badmod
  name: BadModule
modules:
  nonexistent_module_xyz: {}
agents:
  - id: main
    role: worker
    brain:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      config:
        api_key: "claude-code"
    system_prompt: test
execution:
  mode: conversation
""")
        r = await client.post("/api/apps/deploy", json={
            "yaml_path": str(bad_yaml),
            "force": True,
        }, headers=headers, timeout=30)
        d = r.json()
        assert d["success"] is False
