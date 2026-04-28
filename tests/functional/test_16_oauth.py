"""16 - OAuth endpoints for MCP servers."""

import pytest

from .conftest import deploy_app, undeploy_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def app(client, headers):
    await deploy_app(client, "minimal.yaml", headers)
    yield "test-minimal"
    await undeploy_app(client, "test-minimal", headers)


class TestOAuth:
    """MCP OAuth flow endpoints."""

    async def test_pending_oauth(self, client, headers, app):
        r = await client.get(
            f"/api/apps/{app}/mcp/pending-oauth",
            headers=headers,
        )
        assert r.status_code < 500

    async def test_inject_token_nonexistent(self, client, headers, app):
        r = await client.post(
            f"/api/apps/{app}/mcp/nonexistent-server/oauth-token",
            json={"token": "test-token", "expires_at": 9999999999},
            headers=headers,
        )
        assert r.status_code < 500

    async def test_revoke_token_nonexistent(self, client, headers, app):
        r = await client.delete(
            f"/api/apps/{app}/mcp/nonexistent-server/oauth-token",
            headers=headers,
        )
        assert r.status_code < 500

    async def test_authorize_flow(self, client, headers, app):
        r = await client.get(
            f"/api/apps/{app}/oauth/authorize",
            params={"provider": "github", "scopes": "repo"},
            headers=headers,
        )
        # Should redirect or return auth URL - not crash
        assert r.status_code < 500
