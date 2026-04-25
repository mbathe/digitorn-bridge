"""06 — Approval queue: list pending, approve, deny."""

import uuid

import pytest

from .conftest import deploy_app, undeploy_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def security_app(client, headers):
    await deploy_app(client, "security_app.yaml", headers)
    yield "test-security"
    await undeploy_app(client, "test-security", headers)


class TestApprovals:
    """GET/POST /api/apps/{app_id}/approvals, /api/apps/{app_id}/approve"""

    async def test_list_pending_empty(self, client, headers, security_app):
        r = await client.get(
            f"/api/apps/{security_app}/approvals",
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True
        # data is {"pending": [...]}
        pending = d["data"]["pending"]
        assert isinstance(pending, list)
        assert len(pending) == 0  # No pending approvals yet

    async def test_approve_nonexistent(self, client, headers, security_app):
        r = await client.post(f"/api/apps/{security_app}/approve", json={
            "approval_id": "nonexistent-id",
            "approved": True,
        }, headers=headers)
        # Should fail gracefully
        assert r.status_code < 500

    async def test_deny_nonexistent(self, client, headers, security_app):
        r = await client.post(f"/api/apps/{security_app}/approve", json={
            "approval_id": "nonexistent-id",
            "approved": False,
            "message": "Denied for testing",
        }, headers=headers)
        assert r.status_code < 500
