"""01 - Health, readiness, and metrics endpoints."""

import pytest

pytestmark = pytest.mark.asyncio


class TestHealth:
    """GET /health, /healthz, /readyz"""

    async def test_healthz(self, client, headers):
        r = await client.get("/healthz", headers=headers)
        assert r.status_code == 200
        d = r.json()
        assert d["status"] in ("alive", "ok")

    async def test_health(self, client, headers):
        r = await client.get("/health", headers=headers)
        assert r.status_code == 200
        d = r.json()
        assert "status" in d
        assert d["status"] in ("ok", "alive")

    async def test_readyz(self, client, headers):
        r = await client.get("/readyz", headers=headers)
        assert r.status_code == 200
        d = r.json()
        assert "status" in d


class TestMetrics:
    """GET /api/metrics, /api/metrics/prometheus"""

    async def test_metrics_json(self, client, headers):
        r = await client.get("/api/metrics", headers=headers)
        assert r.status_code == 200

    async def test_metrics_prometheus(self, client, headers):
        r = await client.get("/api/metrics/prometheus", headers=headers)
        assert r.status_code == 200
        # Prometheus text format
        assert "text" in r.headers.get("content-type", "")

    async def test_metrics_sessions(self, client, headers):
        r = await client.get("/api/metrics/sessions", headers=headers)
        assert r.status_code == 200
