"""13 — Diagnostics, preview, interact endpoints."""

import pytest

from .conftest import deploy_app, undeploy_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def app(client, headers):
    await deploy_app(client, "minimal.yaml", headers)
    yield "test-minimal"
    await undeploy_app(client, "test-minimal", headers)


class TestDiagnostics:
    """GET /api/apps/{app_id}/diagnostics"""

    async def test_diagnostics(self, client, headers, app):
        r = await client.get(
            f"/api/apps/{app}/diagnostics",
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True
        # Should return check results
        assert d["data"] is not None

    async def test_diagnostics_nonexistent(self, client, headers):
        r = await client.get(
            "/api/apps/nonexistent-xyz/diagnostics",
            headers=headers,
        )
        assert r.status_code in (404, 200)


class TestPreview:
    """GET /api/apps/{app_id}/preview/{buffer_key}"""

    async def test_preview_nonexistent(self, client, headers, app):
        r = await client.get(
            f"/api/apps/{app}/preview/nonexistent-buffer",
            headers=headers,
        )
        assert r.status_code < 500


class TestInteract:
    """POST /api/apps/{app_id}/interact"""

    async def test_interact(self, client, headers, app):
        r = await client.post(f"/api/apps/{app}/interact", json={
            "action": "echo",
            "params": {"message": "test"},
        }, headers=headers)
        assert r.status_code < 500
