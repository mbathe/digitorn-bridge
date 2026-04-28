"""E2E Tests: API endpoints - health, apps, sessions, secrets, deploy."""

import pytest
import httpx
from tests.e2e.conftest import *


class TestAPIHealth:
    def test_health(self, client):
        assert client.health()

    def test_health_response(self, client):
        resp = httpx.get(f"{client.daemon_url}/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data


class TestAPIApps:
    def test_list_apps(self, client):
        resp = httpx.get(f"{client.daemon_url}/api/apps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"]
        assert isinstance(data["data"], list)

    def test_get_app_info(self, client, ensure_deployed):
        resp = httpx.get(f"{client.daemon_url}/api/apps/{client.app_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"]
        assert data["data"]["app_id"] == client.app_id

    def test_get_nonexistent_app(self, client):
        resp = httpx.get(f"{client.daemon_url}/api/apps/nonexistent-xyz")
        assert resp.status_code == 404


class TestAPISessions:
    def test_list_sessions(self, client, ensure_deployed):
        resp = httpx.get(f"{client.daemon_url}/api/apps/{client.app_id}/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"]

    def test_create_session_via_chat(self, client, ensure_deployed):
        import uuid
        sid = f"api-test-{uuid.uuid4().hex[:8]}"
        resp = httpx.post(
            f"{client.daemon_url}/api/apps/{client.app_id}/chat",
            json={"session_id": sid, "message": "Hello"},
            timeout=60.0,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") or data.get("data", {}).get("content")


class TestAPISecrets:
    def test_set_and_list_secret(self, client, ensure_deployed):
        resp = httpx.put(
            f"{client.daemon_url}/api/apps/{client.app_id}/secrets/E2E_TEST_KEY",
            json={"value": "test_value"},
            timeout=10.0,
        )
        assert resp.status_code == 200

        resp2 = httpx.get(
            f"{client.daemon_url}/api/apps/{client.app_id}/secrets",
            timeout=10.0,
        )
        data = resp2.json()
        keys = data.get("data", {}).get("keys", [])
        assert "E2E_TEST_KEY" in keys

        httpx.delete(
            f"{client.daemon_url}/api/apps/{client.app_id}/secrets/E2E_TEST_KEY",
            timeout=10.0,
        )


class TestAPIModules:
    def test_list_modules(self, client):
        resp = httpx.get(f"{client.daemon_url}/api/modules")
        assert resp.status_code == 200
        data = resp.json()
        modules = data.get("modules", data.get("data", {}).get("modules", []))
        ids = [m.get("module_id", m.get("id", "")) for m in modules]
        assert "filesystem" in ids
