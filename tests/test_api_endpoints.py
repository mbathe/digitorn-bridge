"""Unit tests for Digitorn API endpoints.

Tests the routes defined in packages/digitorn/core/api/apps.py and the
top-level health/metrics endpoints in packages/digitorn/core/server.py.

Uses a lightweight FastAPI app with mocked AppManager and RateLimiter —
no database, no module loading, no lifespan overhead.
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from digitorn.core.api.apps import router as apps_router
from digitorn.core.metrics import MetricsCollector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary(app_id: str = "test-app") -> dict:
    return {
        "app_id": app_id,
        "name": "Test App",
        "version": "1.0",
        "mode": "conversation",
        "agents": ["main"],
        "modules": ["filesystem"],
        "total_tools": 5,
        "total_categories": 2,
        "deployed_at": time.time(),
    }


def _make_deployed(app_id: str = "test-app") -> MagicMock:
    deployed = MagicMock()
    deployed.app_id = app_id
    deployed.summary.return_value = _make_summary(app_id)
    deployed.approval_queue = None
    deployed.compiled = MagicMock()
    return deployed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def metrics():
    return MetricsCollector()


@pytest.fixture()
def mock_manager():
    mgr = MagicMock()
    mgr.list_apps.return_value = []
    mgr.is_deployed.return_value = False
    mgr.get.return_value = None
    mgr._deployed = {}
    mgr._secret_store = AsyncMock()
    mgr.deploy = AsyncMock()
    mgr.undeploy = AsyncMock(return_value=False)
    mgr.list_secrets = AsyncMock(return_value=[])
    mgr.get_secret = AsyncMock(return_value=None)
    mgr.set_secret = AsyncMock()
    mgr.delete_secret = AsyncMock(return_value=False)
    return mgr


@pytest.fixture()
def mock_rate_limiter():
    limiter = MagicMock()
    limiter.get_usage.return_value = {"requests": 0, "limit": 60}
    limiter.check.return_value = None
    return limiter


@pytest.fixture()
def app(mock_manager, mock_rate_limiter, metrics):
    """Create a minimal FastAPI app with the apps router and health/metrics."""
    _app = FastAPI()
    _app.include_router(apps_router)

    # Mimic the top-level endpoints from server.py
    @_app.get("/health")
    async def health():
        return {"status": "ok", "version": "1.0.0", "socketio": True}

    @_app.get("/api/metrics")
    async def api_metrics():
        return metrics.snapshot()

    @_app.get("/api/metrics/prometheus")
    async def api_metrics_prometheus():
        from fastapi.responses import Response

        return Response(
            content=metrics.prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # Simulate an authenticated admin user for all test requests
    @_app.middleware("http")
    async def _inject_test_auth(request, call_next):
        request.state.user_id = "test-user"
        request.state.roles = ["admin"]
        request.state.permissions = ["*"]
        return await call_next(request)

    _app.state.app_manager = mock_manager
    _app.state.rate_limiter = mock_rate_limiter
    return _app


@pytest_asyncio.fixture()
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ===================================================================
# 1. Deploy endpoint — POST /api/apps/deploy
# ===================================================================


class TestDeployEndpoint:

    @pytest.mark.asyncio
    async def test_missing_yaml_path_returns_400(self, client):
        resp = await client.post("/api/apps/deploy", json={})
        assert resp.status_code == 400
        assert "yaml_path" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_null_yaml_path_returns_400(self, client):
        resp = await client.post("/api/apps/deploy", json={"yaml_path": None})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_nonexistent_file_returns_404(self, client):
        resp = await client.post(
            "/api/apps/deploy",
            json={"yaml_path": "/tmp/nonexistent_abc123.yaml"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_non_yaml_extension_returns_400(self, client, tmp_path):
        bad_file = tmp_path / "config.json"
        bad_file.write_text("{}")
        resp = await client.post(
            "/api/apps/deploy",
            json={"yaml_path": str(bad_file)},
        )
        assert resp.status_code == 400
        assert ".yaml" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_txt_extension_returns_400(self, client, tmp_path):
        bad_file = tmp_path / "app.txt"
        bad_file.write_text("hello")
        resp = await client.post(
            "/api/apps/deploy",
            json={"yaml_path": str(bad_file)},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_path_traversal_resolved_safely(self, client, tmp_path):
        """Traversal like ../../etc/passwd.yaml should be resolved and then
        rejected (file not found) rather than causing a crash."""
        resp = await client.post(
            "/api/apps/deploy",
            json={"yaml_path": "/tmp/../tmp/../tmp/no_such.yaml"},
        )
        # Path.resolve() collapses traversal — should be a clean 404
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_successful_deploy(self, client, mock_manager, tmp_path):
        yaml_file = tmp_path / "app.yaml"
        yaml_file.write_text("name: test")

        deployed = _make_deployed()
        mock_manager.deploy.return_value = deployed

        resp = await client.post(
            "/api/apps/deploy",
            json={"yaml_path": str(yaml_file)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["app_id"] == "test-app"
        mock_manager.deploy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deploy_with_yml_extension(self, client, mock_manager, tmp_path):
        yaml_file = tmp_path / "app.yml"
        yaml_file.write_text("name: test")

        deployed = _make_deployed()
        mock_manager.deploy.return_value = deployed

        resp = await client.post(
            "/api/apps/deploy",
            json={"yaml_path": str(yaml_file)},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    @pytest.mark.asyncio
    async def test_deploy_exception_returns_error(self, client, mock_manager, tmp_path):
        yaml_file = tmp_path / "broken.yaml"
        yaml_file.write_text("invalid")

        mock_manager.deploy.side_effect = ValueError("Compilation failed")

        resp = await client.post(
            "/api/apps/deploy",
            json={"yaml_path": str(yaml_file)},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "Compilation failed" in body["detail"]

    @pytest.mark.asyncio
    async def test_deploy_with_force_flag(self, client, mock_manager, tmp_path):
        yaml_file = tmp_path / "app.yaml"
        yaml_file.write_text("name: test")
        mock_manager.deploy.return_value = _make_deployed()

        resp = await client.post(
            "/api/apps/deploy",
            json={"yaml_path": str(yaml_file), "force": True},
        )
        assert resp.status_code == 200
        _, kwargs = mock_manager.deploy.call_args
        assert kwargs["force"] is True


# ===================================================================
# 2. Deploy upload — POST /api/apps/deploy/upload
# ===================================================================


class TestDeployUpload:

    @pytest.mark.asyncio
    async def test_file_too_large_returns_413(self, client):
        # Create content bigger than 1 MB
        large_content = b"x" * (1_048_576 + 100)
        resp = await client.post(
            "/api/apps/deploy/upload",
            files={"file": ("big.yaml", io.BytesIO(large_content), "application/yaml")},
        )
        assert resp.status_code == 413
        assert "too large" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_successful_upload(self, client, mock_manager):
        mock_manager.deploy.return_value = _make_deployed()
        content = b"name: test\nversion: '1.0'"

        resp = await client.post(
            "/api/apps/deploy/upload",
            files={"file": ("app.yaml", io.BytesIO(content), "application/yaml")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["app_id"] == "test-app"

    @pytest.mark.asyncio
    async def test_upload_deploy_failure(self, client, mock_manager):
        mock_manager.deploy.side_effect = RuntimeError("bad yaml")
        content = b"invalid: {{"

        resp = await client.post(
            "/api/apps/deploy/upload",
            files={"file": ("bad.yaml", io.BytesIO(content), "application/yaml")},
        )
        assert resp.status_code == 500
        body = resp.json()
        assert "Internal error during deployment" in body["detail"]


# ===================================================================
# 3. List apps — GET /api/apps
# ===================================================================


class TestListApps:

    @pytest.mark.asyncio
    async def test_empty_list(self, client, mock_manager):
        mock_manager.list_apps.return_value = []
        resp = await client.get("/api/apps")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"] == []

    @pytest.mark.asyncio
    async def test_list_with_apps(self, client, mock_manager):
        mock_manager.list_apps.return_value = [
            _make_summary("app-a"),
            _make_summary("app-b"),
        ]
        resp = await client.get("/api/apps")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 2
        assert data[0]["app_id"] == "app-a"
        assert data[1]["app_id"] == "app-b"


# ===================================================================
# 4. Get app — GET /api/apps/{app_id}
# ===================================================================


class TestGetApp:

    @pytest.mark.asyncio
    async def test_not_deployed_returns_404(self, client, mock_manager):
        mock_manager.get.return_value = None
        resp = await client.get("/api/apps/missing")
        assert resp.status_code == 404
        assert "not deployed" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_get_deployed_app(self, client, mock_manager):
        mock_manager.get.return_value = _make_deployed("my-app")
        resp = await client.get("/api/apps/my-app")
        assert resp.status_code == 200
        assert resp.json()["data"]["app_id"] == "my-app"


# ===================================================================
# 5. Undeploy — DELETE /api/apps/{app_id}
# ===================================================================


class TestUndeploy:

    @pytest.mark.asyncio
    async def test_undeploy_not_found(self, client, mock_manager):
        mock_manager.undeploy.return_value = False
        resp = await client.delete("/api/apps/ghost")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_undeploy_success(self, client, mock_manager):
        mock_manager.undeploy.return_value = True
        resp = await client.delete("/api/apps/my-app")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["undeployed"] is True


# ===================================================================
# 6. Health endpoint — GET /health
# ===================================================================


class TestHealth:

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "version" in body

    @pytest.mark.asyncio
    async def test_health_includes_version(self, client):
        resp = await client.get("/health")
        body = resp.json()
        assert body["version"] == "1.0.0"


# ===================================================================
# 7. Metrics — GET /api/metrics
# ===================================================================


class TestMetrics:

    @pytest.mark.asyncio
    async def test_metrics_returns_json(self, client):
        resp = await client.get("/api/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert "counters" in body
        assert "gauges" in body
        assert "histograms" in body

    @pytest.mark.asyncio
    async def test_metrics_has_uptime(self, client):
        resp = await client.get("/api/metrics")
        body = resp.json()
        assert "uptime_seconds" in body
        assert body["uptime_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_metrics_after_counter_inc(self, client, metrics):
        metrics.inc("test_counter_total", scope="unit")
        resp = await client.get("/api/metrics")
        body = resp.json()
        assert "test_counter_total" in body["counters"]


# ===================================================================
# 8. Prometheus metrics — GET /api/metrics/prometheus
# ===================================================================


class TestPrometheusMetrics:

    @pytest.mark.asyncio
    async def test_prometheus_content_type(self, client):
        resp = await client.get("/api/metrics/prometheus")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_prometheus_contains_type_help(self, client, metrics):
        # Populate at least one metric so there is output
        metrics.inc("http_requests_total", status="200")
        resp = await client.get("/api/metrics/prometheus")
        text = resp.text
        assert "# TYPE" in text
        assert "digitorn_" in text

    @pytest.mark.asyncio
    async def test_prometheus_uptime_gauge(self, client):
        resp = await client.get("/api/metrics/prometheus")
        text = resp.text
        assert "digitorn_uptime_seconds" in text
        assert "# HELP digitorn_uptime_seconds" in text


# ===================================================================
# 9. Quota / Rate limiting endpoints
# ===================================================================


class TestQuota:

    @pytest.mark.asyncio
    async def test_get_quota(self, client, mock_rate_limiter):
        mock_rate_limiter.get_usage.return_value = {"requests": 5, "limit": 60}
        resp = await client.get("/api/apps/my-app/quota")
        assert resp.status_code == 200
        assert resp.json()["data"]["requests"] == 5

    @pytest.mark.asyncio
    async def test_set_quota(self, client, mock_rate_limiter):
        resp = await client.put(
            "/api/apps/my-app/quota", json={"rpm": 120}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["rpm"] == 120
        mock_rate_limiter.set_quota.assert_called_once_with("my-app", 120)

    @pytest.mark.asyncio
    async def test_delete_quota(self, client, mock_rate_limiter):
        resp = await client.delete("/api/apps/my-app/quota")
        assert resp.status_code == 200
        assert resp.json()["data"]["quota"] == "default"
        mock_rate_limiter.remove_quota.assert_called_once_with("my-app")


# ===================================================================
# 10. Approvals
# ===================================================================


class TestApprovals:

    @pytest.mark.asyncio
    async def test_list_approvals_no_queue(self, client, mock_manager):
        deployed = _make_deployed()
        deployed.approval_queue = None
        mock_manager.is_deployed.return_value = True
        mock_manager.get.return_value = deployed

        resp = await client.get("/api/apps/test-app/approvals")
        assert resp.status_code == 200
        assert resp.json()["data"]["pending"] == []

    @pytest.mark.asyncio
    async def test_list_approvals_not_deployed(self, client, mock_manager):
        mock_manager.is_deployed.return_value = False
        resp = await client.get("/api/apps/ghost/approvals")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_approvals_with_queue(self, client, mock_manager):
        deployed = _make_deployed()
        aq = MagicMock()
        aq.list_pending_for_user.return_value = [{"request_id": "r1", "action": "rm"}]
        deployed.approval_queue = aq
        mock_manager.is_deployed.return_value = True
        mock_manager.get.return_value = deployed

        resp = await client.get("/api/apps/test-app/approvals")
        assert resp.status_code == 200
        assert len(resp.json()["data"]["pending"]) == 1


# ===================================================================
# 11. Secrets
# ===================================================================


class TestSecrets:

    @pytest.mark.asyncio
    async def test_list_secrets(self, client, mock_manager):
        mock_manager.list_secrets.return_value = ["API_KEY", "DB_PASS"]
        resp = await client.get("/api/apps/my-app/secrets")
        assert resp.status_code == 200
        assert resp.json()["data"]["keys"] == ["API_KEY", "DB_PASS"]

    @pytest.mark.asyncio
    async def test_check_secret_exists(self, client, mock_manager):
        mock_manager.get_secret.return_value = "s3cret"
        resp = await client.get("/api/apps/my-app/secrets/API_KEY")
        assert resp.status_code == 200
        assert resp.json()["data"]["exists"] is True

    @pytest.mark.asyncio
    async def test_check_secret_not_found(self, client, mock_manager):
        mock_manager.get_secret.return_value = None
        resp = await client.get("/api/apps/my-app/secrets/MISSING")
        assert resp.status_code == 200
        assert resp.json()["data"]["exists"] is False

    @pytest.mark.asyncio
    async def test_set_secret(self, client, mock_manager):
        resp = await client.put(
            "/api/apps/my-app/secrets/TOKEN",
            json={"value": "abc123"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "set"
        mock_manager.set_secret.assert_awaited_once_with(
            "my-app", "TOKEN", "abc123",
        )

    @pytest.mark.asyncio
    async def test_delete_secret(self, client, mock_manager):
        mock_manager.delete_secret.return_value = True
        resp = await client.delete("/api/apps/my-app/secrets/TOKEN")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_delete_secret_not_found(self, client, mock_manager):
        mock_manager.delete_secret.return_value = False
        resp = await client.delete("/api/apps/my-app/secrets/GONE")
        assert resp.status_code == 404


# ===================================================================
# 12. Manager not available (503)
# ===================================================================


class TestManagerNotAvailable:

    @pytest.mark.asyncio
    async def test_no_manager_returns_503(self):
        """When app_manager is not set, endpoints should return 503."""
        bare_app = FastAPI()
        bare_app.include_router(apps_router)
        # Deliberately do NOT set app.state.app_manager

        transport = ASGITransport(app=bare_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/apps")
            assert resp.status_code == 503
            assert "AppManager" in resp.json()["detail"]
