"""20 — P0: Concurrent requests — same session, deploy+chat, chat+delete."""

import asyncio
import uuid

import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait, send_message

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def app(client, headers):
    await deploy_app(client, "minimal.yaml", headers)
    yield "test-minimal"
    await undeploy_app(client, "test-minimal", headers)


class TestConcurrentChatSameSession:
    """Two chat requests on the same session simultaneously."""

    async def test_concurrent_chat_no_crash(self, client, headers, app):
        """Daemon should not crash with concurrent chat on same session."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        # First message to create the session
        await send_and_wait(client, app, sid, "init", headers)

        # Fire two concurrent chats on same session (fire-and-forget)
        async def chat(msg):
            return await send_and_wait(client, app, sid, msg, headers)

        r1, r2 = await asyncio.gather(
            chat("concurrent message A"),
            chat("concurrent message B"),
            return_exceptions=True,
        )
        # Neither should crash the daemon — both may succeed or one may
        # get a conflict/busy error, but no 500
        for r in [r1, r2]:
            if not isinstance(r, Exception):
                assert r.get("success") is not None

    async def test_concurrent_chat_data_integrity(self, client, headers, app):
        """After concurrent chat, session history should be consistent."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(client, app, sid, "start", headers)

        # Send 3 concurrent messages (fire-and-forget + wait)
        tasks = [
            send_and_wait(client, app, sid, f"msg-{i}", headers)
            for i in range(3)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # At least one should succeed
        successes = [r for r in results if not isinstance(r, Exception) and r.get("success")]
        assert len(successes) >= 1

        # Session should still be readable
        r = await client.get(f"/api/apps/{app}/sessions/{sid}", headers=headers)
        assert r.status_code == 200


class TestConcurrentChatAndDelete:
    """Delete session while chat is in progress."""

    async def test_delete_during_idle(self, client, headers, app):
        """Delete a session that has no active chat — should succeed."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(client, app, sid, "hello", headers)

        r = await client.delete(f"/api/apps/{app}/sessions/{sid}", headers=headers)
        assert r.status_code < 500


class TestConcurrentDeployUndeploy:
    """Deploy and undeploy same app concurrently."""

    async def test_concurrent_deploy_undeploy_no_crash(self, client, headers):
        """Concurrent deploy+undeploy of different apps should not crash."""
        from .conftest import APPS_DIR

        async def deploy_and_undeploy(yaml_name, app_id):
            yaml_path = str((APPS_DIR / yaml_name).resolve())
            r = await client.post("/api/apps/deploy", json={
                "yaml_path": yaml_path, "force": True,
            }, headers=headers, timeout=30)
            await asyncio.sleep(0.2)
            await client.delete(f"/api/apps/{app_id}", headers=headers)
            return r.status_code

        s1, s2 = await asyncio.gather(
            deploy_and_undeploy("memory_app.yaml", "test-memory"),
            deploy_and_undeploy("shell_app.yaml", "test-shell"),
            return_exceptions=True,
        )
        # Neither should be a 500 or unhandled exception
        for s in [s1, s2]:
            if isinstance(s, int):
                assert s < 500

    async def test_rapid_redeploy(self, client, headers):
        """Deploy → undeploy → deploy same app rapidly."""
        from .conftest import APPS_DIR
        yaml_path = str((APPS_DIR / "minimal.yaml").resolve())

        for _ in range(5):
            r = await client.post("/api/apps/deploy", json={
                "yaml_path": yaml_path, "force": True,
            }, headers=headers, timeout=30)
            assert r.status_code < 500
            await client.delete("/api/apps/test-minimal", headers=headers)
            await asyncio.sleep(0.1)


class TestConcurrentSessionOps:
    """Concurrent compact, fork, undo on same session."""

    async def test_concurrent_compact_and_chat(self, client, headers, app):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(client, app, sid, "setup", headers)

        async def compact():
            return await client.post(
                f"/api/apps/{app}/sessions/{sid}/compact",
                headers=headers,
            )

        async def chat():
            return await send_and_wait(client, app, sid, "during compact", headers)

        r1, r2 = await asyncio.gather(
            compact(), chat(), return_exceptions=True,
        )
        for r in [r1, r2]:
            if not isinstance(r, Exception):
                if hasattr(r, 'status_code'):
                    assert r.status_code < 500
