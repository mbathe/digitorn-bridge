"""14 - One-shot execution and pipeline endpoints."""

import pytest

from .conftest import deploy_app, undeploy_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def oneshot_app(client, headers):
    await deploy_app(client, "oneshot_app.yaml", headers)
    yield "test-oneshot"
    await undeploy_app(client, "test-oneshot", headers)


class TestOneShot:
    """POST /api/apps/{app_id}/run"""

    async def test_run(self, client, headers, oneshot_app):
        r = await client.post(f"/api/apps/{oneshot_app}/run", json={
            "input": "Hello World",
            "input_type": "text",
        }, headers=headers, timeout=120)
        d = r.json()
        assert d["success"] is True
        assert d["data"] is not None

    async def test_run_empty_input(self, client, headers, oneshot_app):
        r = await client.post(f"/api/apps/{oneshot_app}/run", json={
            "input": "",
            "input_type": "text",
        }, headers=headers, timeout=120)
        # Should handle gracefully
        assert r.status_code < 500

    async def test_run_nonexistent_app(self, client, headers):
        r = await client.post("/api/apps/nonexistent-xyz/run", json={
            "input": "test",
        }, headers=headers)
        assert r.status_code == 404 or r.json().get("success") is False


class TestPipeline:
    """POST /api/apps/{app_id}/pipeline"""

    async def test_pipeline_nonexistent(self, client, headers):
        r = await client.post("/api/apps/nonexistent-xyz/pipeline", json={
            "input": "test",
            "steps": [],
        }, headers=headers)
        assert r.status_code < 500
