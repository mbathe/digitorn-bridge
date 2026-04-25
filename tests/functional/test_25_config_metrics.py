"""25 — P1: Config PATCH, metrics per-session, metrics per-app, pipeline."""

import uuid

import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait

pytestmark = pytest.mark.asyncio


class TestConfigPatch:
    """PATCH /api/config"""

    async def test_patch_empty(self, client, headers):
        """Empty patch should be a no-op."""
        r = await client.patch("/api/config", json={}, headers=headers)
        assert r.status_code < 500

    async def test_patch_logging_level(self, client, headers):
        """Change logging level at runtime."""
        r = await client.patch("/api/config", json={
            "logging": {"level": "warning"},
        }, headers=headers)
        assert r.status_code < 500

    async def test_patch_invalid_field(self, client, headers):
        """Patch with unknown field."""
        r = await client.patch("/api/config", json={
            "nonexistent_field": {"value": 42},
        }, headers=headers)
        assert r.status_code < 500

    async def test_get_config(self, client, headers):
        """GET /api/config should return current config."""
        r = await client.get("/api/config", headers=headers)
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d, dict)


class TestMetricsPerSession:
    """GET /api/metrics/sessions/{session_id}"""

    async def test_metrics_session_after_chat(self, client, headers):
        await deploy_app(client, "minimal.yaml", headers)
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(client, "test-minimal", sid, "hello", headers)

        r = await client.get(f"/api/metrics/sessions/{sid}", headers=headers)
        assert r.status_code < 500
        await undeploy_app(client, "test-minimal", headers)

    async def test_metrics_session_nonexistent(self, client, headers):
        r = await client.get("/api/metrics/sessions/nonexistent-xyz", headers=headers)
        assert r.status_code < 500


class TestMetricsPerApp:
    """GET /api/metrics/apps/{app_id}"""

    async def test_metrics_app(self, client, headers):
        await deploy_app(client, "minimal.yaml", headers)
        r = await client.get("/api/metrics/apps/test-minimal", headers=headers)
        assert r.status_code < 500
        await undeploy_app(client, "test-minimal", headers)

    async def test_metrics_app_nonexistent(self, client, headers):
        r = await client.get("/api/metrics/apps/nonexistent-xyz", headers=headers)
        assert r.status_code < 500


class TestPipeline:
    """POST /api/apps/{app_id}/pipeline"""

    async def test_pipeline_empty_steps(self, client, headers):
        await deploy_app(client, "oneshot_app.yaml", headers)
        r = await client.post("/api/apps/test-oneshot/pipeline", json={
            "input": "test",
            "steps": [],
        }, headers=headers, timeout=120)
        assert r.status_code < 500
        await undeploy_app(client, "test-oneshot", headers)

    async def test_pipeline_nonexistent_app(self, client, headers):
        r = await client.post("/api/apps/nonexistent-xyz/pipeline", json={
            "input": "test",
            "steps": [{"app": "nonexistent"}],
        }, headers=headers)
        assert r.status_code < 500
