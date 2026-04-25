"""09 — Config API and Modules API."""

import pytest

from .conftest import deploy_app, undeploy_app

pytestmark = pytest.mark.asyncio


class TestConfigAPI:
    """GET/PATCH /api/config"""

    async def test_get_config(self, client, headers):
        r = await client.get("/api/config", headers=headers)
        d = r.json()
        assert r.status_code == 200
        # Should return some config structure
        assert isinstance(d, dict)

    async def test_browse_filesystem(self, client, headers):
        r = await client.get(
            "/api/config/browse",
            params={"path": "."},
            headers=headers,
        )
        assert r.status_code < 500


class TestModulesAPI:
    """GET /api/modules"""

    async def test_list_modules(self, client, headers):
        r = await client.get("/api/modules", headers=headers)
        d = r.json()
        assert r.status_code == 200
        # Should have modules list
        assert "modules" in d or isinstance(d, list)

    async def test_get_module_detail(self, client, headers):
        r = await client.get("/api/modules/filesystem", headers=headers)
        assert r.status_code < 500
        d = r.json()
        if r.status_code == 200:
            assert "module_id" in d or "filesystem" in str(d)

    async def test_module_health(self, client, headers):
        r = await client.get("/api/modules/filesystem/health", headers=headers)
        assert r.status_code < 500

    async def test_module_execute(self, client, headers):
        """Direct module action execution."""
        await deploy_app(client, "filesystem_app.yaml", headers)
        r = await client.post("/api/modules/filesystem/execute", json={
            "action": "ls",
            "params": {"path": "."},
            "app_id": "test-filesystem",
        }, headers=headers)
        assert r.status_code < 500
        await undeploy_app(client, "test-filesystem", headers)

    async def test_get_nonexistent_module(self, client, headers):
        r = await client.get("/api/modules/nonexistent_module_xyz", headers=headers)
        assert r.status_code in (404, 200)
