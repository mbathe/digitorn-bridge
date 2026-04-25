"""08 — Secrets management and rate limiting / quotas."""

import pytest

from .conftest import deploy_app, undeploy_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def app(client, headers):
    await deploy_app(client, "minimal.yaml", headers)
    yield "test-minimal"
    await undeploy_app(client, "test-minimal", headers)


class TestSecrets:
    """CRUD /api/apps/{app_id}/secrets"""

    async def test_list_secrets_empty(self, client, headers, app):
        r = await client.get(f"/api/apps/{app}/secrets", headers=headers)
        d = r.json()
        assert d["success"] is True

    async def test_set_secret(self, client, headers, app):
        r = await client.put(
            f"/api/apps/{app}/secrets/MY_TEST_KEY",
            json={"value": "secret-value-123"},
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True

    async def test_check_secret_exists(self, client, headers, app):
        # Set first
        await client.put(
            f"/api/apps/{app}/secrets/CHECK_KEY",
            json={"value": "val"},
            headers=headers,
        )
        r = await client.get(
            f"/api/apps/{app}/secrets/CHECK_KEY",
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True

    async def test_delete_secret(self, client, headers, app):
        await client.put(
            f"/api/apps/{app}/secrets/DEL_KEY",
            json={"value": "val"},
            headers=headers,
        )
        r = await client.delete(
            f"/api/apps/{app}/secrets/DEL_KEY",
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True

    async def test_get_nonexistent_secret(self, client, headers, app):
        r = await client.get(
            f"/api/apps/{app}/secrets/NONEXISTENT_KEY_XYZ",
            headers=headers,
        )
        d = r.json()
        # Should indicate not found
        assert r.status_code < 500


class TestQuotas:
    """CRUD /api/apps/{app_id}/quota"""

    async def test_get_quota(self, client, headers, app):
        r = await client.get(f"/api/apps/{app}/quota", headers=headers)
        d = r.json()
        assert d["success"] is True

    async def test_set_quota(self, client, headers, app):
        r = await client.put(
            f"/api/apps/{app}/quota",
            json={"limit": 100},
            headers=headers,
        )
        assert r.status_code < 500

    async def test_reset_quota(self, client, headers, app):
        r = await client.delete(f"/api/apps/{app}/quota", headers=headers)
        assert r.status_code < 500

    async def test_get_user_quota(self, client, headers, app):
        r = await client.get(
            f"/api/apps/{app}/quota/user/testuser",
            headers=headers,
        )
        assert r.status_code < 500

    async def test_set_user_quota(self, client, headers, app):
        r = await client.put(
            f"/api/apps/{app}/quota/user/testuser",
            json={"limit": 50},
            headers=headers,
        )
        assert r.status_code < 500

    async def test_reset_user_quota(self, client, headers, app):
        r = await client.delete(
            f"/api/apps/{app}/quota/user/testuser",
            headers=headers,
        )
        assert r.status_code < 500
