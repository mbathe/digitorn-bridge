"""11 - Security profiles API: create, get, update, delete, grants."""

import pytest

from .conftest import deploy_app, undeploy_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def sec_app(client, headers):
    await deploy_app(client, "security_app.yaml", headers)
    yield "test-security"
    await undeploy_app(client, "test-security", headers)


class TestSecurityProfiles:
    """CRUD /api/security/profiles"""

    async def test_get_profile(self, client, headers, sec_app):
        r = await client.get(
            f"/api/security/profiles/{sec_app}",
            headers=headers,
        )
        assert r.status_code < 500

    async def test_get_grants(self, client, headers, sec_app):
        r = await client.get(
            f"/api/security/profiles/{sec_app}/grants",
            headers=headers,
        )
        assert r.status_code < 500

    async def test_create_profile(self, client, headers):
        r = await client.post("/api/security/profiles", json={
            "app_id": "test-profile-create",
            "default_policy": "block",
            "max_risk_level": "medium",
        }, headers=headers)
        assert r.status_code < 500
        # Cleanup
        await client.delete(
            "/api/security/profiles/test-profile-create",
            headers=headers,
        )

    async def test_update_profile(self, client, headers, sec_app):
        r = await client.patch(
            f"/api/security/profiles/{sec_app}",
            json={"max_risk_level": "high"},
            headers=headers,
        )
        assert r.status_code < 500

    async def test_delete_nonexistent_profile(self, client, headers):
        r = await client.delete(
            "/api/security/profiles/nonexistent-app-xyz",
            headers=headers,
        )
        assert r.status_code < 500

    async def test_upsert_grant(self, client, headers, sec_app):
        r = await client.put(
            f"/api/security/profiles/{sec_app}/grants/memory",
            json={"actions": ["set_goal", "remember"], "policy": "auto"},
            headers=headers,
        )
        assert r.status_code < 500

    async def test_delete_grant(self, client, headers, sec_app):
        r = await client.delete(
            f"/api/security/profiles/{sec_app}/grants/memory",
            headers=headers,
        )
        assert r.status_code < 500
