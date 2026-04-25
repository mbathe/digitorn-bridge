"""02 — App deployment, listing, details, undeploy."""

import pytest
from pathlib import Path

from .conftest import APPS_DIR, deploy_app, undeploy_app

pytestmark = pytest.mark.asyncio


class TestDeploy:
    """POST /api/apps/deploy"""

    async def test_deploy_from_path(self, client, headers):
        data = await deploy_app(client, "minimal.yaml", headers)
        assert data["success"] is True
        # Cleanup
        await undeploy_app(client, "test-minimal", headers)

    async def test_deploy_force_redeploy(self, client, headers):
        """Deploy same app twice with force=True should succeed."""
        await deploy_app(client, "minimal.yaml", headers)
        data = await deploy_app(client, "minimal.yaml", headers)
        assert data["success"] is True
        await undeploy_app(client, "test-minimal", headers)

    async def test_deploy_invalid_path(self, client, headers):
        r = await client.post("/api/apps/deploy", json={
            "yaml_path": "/nonexistent/path/app.yaml",
            "force": False,
        }, headers=headers)
        d = r.json()
        assert d["success"] is False or r.status_code >= 400

    async def test_deploy_upload(self, client, headers):
        """POST /api/apps/deploy/upload — upload YAML file."""
        yaml_path = APPS_DIR / "minimal.yaml"
        with open(yaml_path, "rb") as f:
            r = await client.post(
                "/api/apps/deploy/upload",
                files={"file": ("minimal.yaml", f, "application/x-yaml")},
                headers=headers,
            )
        d = r.json()
        # May succeed or fail depending on implementation; at minimum shouldn't 500
        assert r.status_code < 500
        if d.get("success"):
            await undeploy_app(client, "test-minimal", headers)


class TestAppList:
    """GET /api/apps/"""

    async def test_list_empty(self, client, headers):
        r = await client.get("/api/apps", headers=headers)
        assert r.status_code == 200
        d = r.json()
        assert d["success"] is True
        assert isinstance(d["data"], list)

    async def test_list_after_deploy(self, client, headers):
        await deploy_app(client, "minimal.yaml", headers)
        r = await client.get("/api/apps", headers=headers)
        d = r.json()
        assert d["success"] is True
        app_ids = [a["app_id"] for a in d["data"]]
        assert "test-minimal" in app_ids
        await undeploy_app(client, "test-minimal", headers)


class TestAppDetails:
    """GET /api/apps/{app_id}"""

    async def test_get_details(self, client, headers):
        await deploy_app(client, "minimal.yaml", headers)
        r = await client.get("/api/apps/test-minimal", headers=headers)
        d = r.json()
        assert d["success"] is True
        assert d["data"]["app_id"] == "test-minimal"
        assert d["data"]["name"] == "Test Minimal"
        assert "agents" in d["data"]
        assert "modules" in d["data"]
        await undeploy_app(client, "test-minimal", headers)

    async def test_get_nonexistent(self, client, headers):
        r = await client.get("/api/apps/nonexistent-app-xyz", headers=headers)
        assert r.status_code == 404 or r.json().get("success") is False


class TestUndeploy:
    """DELETE /api/apps/{app_id}"""

    async def test_undeploy(self, client, headers):
        await deploy_app(client, "minimal.yaml", headers)
        r = await client.delete("/api/apps/test-minimal", headers=headers)
        d = r.json()
        assert d["success"] is True

        # Verify gone
        r2 = await client.get("/api/apps/test-minimal", headers=headers)
        assert r2.status_code == 404 or r2.json().get("success") is False

    async def test_undeploy_nonexistent(self, client, headers):
        r = await client.delete("/api/apps/nonexistent-xyz", headers=headers)
        assert r.status_code == 404 or r.json().get("success") is False
