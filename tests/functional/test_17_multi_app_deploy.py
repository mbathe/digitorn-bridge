"""17 — Multi-app deployment: deploy several apps, verify isolation."""

import uuid

import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait

pytestmark = pytest.mark.asyncio


class TestMultiAppDeploy:
    """Deploy multiple apps simultaneously and verify isolation."""

    async def test_deploy_multiple(self, client, headers):
        """Deploy minimal + memory + shell apps together."""
        d1 = await deploy_app(client, "minimal.yaml", headers)
        d2 = await deploy_app(client, "memory_app.yaml", headers)
        d3 = await deploy_app(client, "shell_app.yaml", headers)

        assert d1["success"] is True
        assert d2["success"] is True
        assert d3["success"] is True

        # List should show all 3
        r = await client.get("/api/apps", headers=headers)
        data = r.json()["data"]
        # data may be a list of app summaries directly
        app_list = data if isinstance(data, list) else data.get("apps", [])
        app_ids = [a["app_id"] for a in app_list]
        assert "test-minimal" in app_ids
        assert "test-memory" in app_ids
        assert "test-shell" in app_ids

        # Cleanup
        await undeploy_app(client, "test-minimal", headers)
        await undeploy_app(client, "test-memory", headers)
        await undeploy_app(client, "test-shell", headers)

    async def test_session_isolation(self, client, headers):
        """Sessions in one app don't leak to another."""
        await deploy_app(client, "minimal.yaml", headers)
        await deploy_app(client, "memory_app.yaml", headers)

        sid = f"test-{uuid.uuid4().hex[:8]}"
        # Chat in minimal app
        await send_and_wait(client, "test-minimal", sid, "test", headers)

        # Session should NOT appear in memory app
        r = await client.get("/api/apps/test-memory/sessions", headers=headers)
        d = r.json()
        if d["success"] and d["data"]:
            sessions = d["data"].get("sessions", []) if isinstance(d["data"], dict) else d["data"]
            session_ids = [s.get("session_id", "") for s in sessions]
            assert sid not in session_ids

        await undeploy_app(client, "test-minimal", headers)
        await undeploy_app(client, "test-memory", headers)

    async def test_undeploy_one_keeps_others(self, client, headers):
        """Undeploying one app doesn't affect others."""
        await deploy_app(client, "minimal.yaml", headers)
        await deploy_app(client, "memory_app.yaml", headers)

        await undeploy_app(client, "test-minimal", headers)

        # memory app should still work
        r = await client.get("/api/apps/test-memory", headers=headers)
        assert r.json()["success"] is True

        await undeploy_app(client, "test-memory", headers)
