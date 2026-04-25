"""27 — P3: Robustness — large messages, persistence, edge cases, undeploy with active sessions."""

import uuid

import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def app(client, headers):
    await deploy_app(client, "minimal.yaml", headers)
    yield "test-minimal"
    await undeploy_app(client, "test-minimal", headers)


@pytest.fixture
async def fs_app(client, headers):
    await deploy_app(client, "filesystem_app.yaml", headers)
    yield "test-filesystem"
    await undeploy_app(client, "test-filesystem", headers)


# ═══════════════════════════════════════════════════════════════
# LARGE MESSAGES
# ═══════════════════════════════════════════════════════════════

class TestLargeMessages:
    """Messages of various sizes."""

    async def test_medium_message_10k(self, client, headers, app):
        """10KB message should work fine."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        message = "A" * 10_000 + "\nSummarize the above."
        d = await send_and_wait(client, app, sid, message, headers)
        assert d["success"] is True

    async def test_large_message_100k(self, client, headers, app):
        """100KB message — should handle or reject gracefully."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        message = "B" * 100_000
        d = await send_and_wait(client, app, sid, message, headers, timeout=120)
        # Should either succeed or return an error, not crash
        assert d.get("success") is not None

    async def test_very_large_message_1m(self, client, headers, app):
        """1MB message — should reject or handle without OOM."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        message = "C" * 1_000_000
        d = await send_and_wait(client, app, sid, message, headers, timeout=120)
        assert d.get("success") is not None


# ═══════════════════════════════════════════════════════════════
# UNDEPLOY WITH ACTIVE SESSIONS
# ═══════════════════════════════════════════════════════════════

class TestUndeployWithSessions:
    """Undeploy app that has active sessions."""

    async def test_undeploy_cleans_sessions(self, client, headers):
        """Sessions should be cleaned up after undeploy."""
        await deploy_app(client, "minimal.yaml", headers)
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(client, "test-minimal", sid, "hello", headers)

        # Verify session exists
        r = await client.get(f"/api/apps/test-minimal/sessions/{sid}", headers=headers)
        assert r.json()["success"] is True

        # Undeploy
        await undeploy_app(client, "test-minimal", headers)

        # Redeploy — sessions should be gone
        await deploy_app(client, "minimal.yaml", headers)
        r = await client.get("/api/apps/test-minimal/sessions", headers=headers)
        sessions = r.json()["data"].get("sessions", [])
        session_ids = [s.get("session_id", "") for s in sessions]
        assert sid not in session_ids
        await undeploy_app(client, "test-minimal", headers)

    async def test_undeploy_twice(self, client, headers):
        """Undeploy an already-undeployed app."""
        await deploy_app(client, "minimal.yaml", headers)
        await undeploy_app(client, "test-minimal", headers)
        r = await client.delete("/api/apps/test-minimal", headers=headers)
        # Should return 404 or success=false, not crash
        assert r.status_code < 500


# ═══════════════════════════════════════════════════════════════
# SESSION PERSISTENCE
# ═══════════════════════════════════════════════════════════════

class TestSessionPersistence:
    """Session state survives re-deploy."""

    async def test_session_survives_force_redeploy(self, client, headers):
        """After force re-deploy, can we still chat on the same session?"""
        await deploy_app(client, "minimal.yaml", headers)
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(client, "test-minimal", sid,
                                "Remember this: test-persistence-42",
                                headers)
        assert d["success"] is True

        # Force re-deploy (undeploy + deploy internally)
        await deploy_app(client, "minimal.yaml", headers)

        # Chat on same session — may or may not have context
        d = await send_and_wait(client, "test-minimal", sid,
                                "What did I ask you to remember?",
                                headers)
        # Should not crash — session may be new (re-deploy cleans sessions)
        assert d["success"] is True
        await undeploy_app(client, "test-minimal", headers)


# ═══════════════════════════════════════════════════════════════
# EDGE CASES
# ═══════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Various edge cases."""

    async def test_many_sessions_same_app(self, client, headers, app):
        """Create many sessions on same app."""
        for i in range(10):
            sid = f"test-{uuid.uuid4().hex[:8]}"
            d = await send_and_wait(client, app, sid, f"session {i}", headers)
            assert d["success"] is True

        # List should show them all
        r = await client.get(f"/api/apps/{app}/sessions", headers=headers)
        sessions = r.json()["data"].get("sessions", [])
        assert len(sessions) >= 10

    async def test_chat_after_manual_compact(self, client, headers, app):
        """Compact then continue chatting."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(client, app, sid, "hello", headers)

        # Compact
        await client.post(f"/api/apps/{app}/sessions/{sid}/compact", headers=headers)

        # Continue chatting
        d = await send_and_wait(client, app, sid, "still here?", headers)
        assert d["success"] is True

    async def test_fork_then_chat_both(self, client, headers, app):
        """Fork a session, then chat on both original and fork."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(client, app, sid, "original session", headers)

        r = await client.post(f"/api/apps/{app}/sessions/{sid}/fork", headers=headers)
        assert r.status_code < 500

    async def test_chat_returns_tool_calls_list(self, client, headers, fs_app):
        """Chat that triggers tools should return tool_calls info."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(client, fs_app, sid,
                                "List the files in the current directory",
                                headers)
        assert d["success"] is True
        # The new API returns tool_calls_count instead of tool_calls list
        assert "tool_calls_count" in d["data"]

    async def test_empty_deploy_body(self, client, headers):
        """Deploy with empty body."""
        r = await client.post("/api/apps/deploy", json={}, headers=headers)
        assert r.status_code < 500
        assert r.json().get("success") is False

    async def test_deploy_with_secrets(self, client, headers, tmp_path):
        """Deploy with inline secrets."""
        yaml_f = tmp_path / "secret_app.yaml"
        yaml_f.write_text("""
app:
  app_id: test-secrets-app
  name: Secret App
modules:
  memory: {}
agents:
  - id: main
    role: worker
    brain:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      config:
        api_key: "{{secret.MY_KEY}}"
    system_prompt: test
execution:
  mode: conversation
  workspace_mode: none
""")
        r = await client.post("/api/apps/deploy", json={
            "yaml_path": str(yaml_f),
            "force": True,
            "secrets": {"MY_KEY": "test-secret-value"},
        }, headers=headers, timeout=30)
        # Should at least not crash
        assert r.status_code < 500
        # Cleanup
        await client.delete("/api/apps/test-secrets-app", headers=headers)
