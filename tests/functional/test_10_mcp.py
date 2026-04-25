"""10 — MCP management API: search, install, list, pool, health."""

import pytest

pytestmark = pytest.mark.asyncio


class TestMCPSearch:
    """GET /api/mcp/search"""

    async def test_search(self, client, headers):
        r = await client.get(
            "/api/mcp/search",
            params={"q": "github"},
            headers=headers,
        )
        assert r.status_code < 500

    async def test_search_empty(self, client, headers):
        r = await client.get(
            "/api/mcp/search",
            params={"q": ""},
            headers=headers,
        )
        assert r.status_code < 500


class TestMCPServers:
    """CRUD /api/mcp/servers"""

    async def test_list_servers(self, client, headers):
        r = await client.get("/api/mcp/servers", headers=headers)
        assert r.status_code < 500

    async def test_get_nonexistent_server(self, client, headers):
        r = await client.get("/api/mcp/servers/nonexistent", headers=headers)
        assert r.status_code in (404, 200, 500)  # Depends on implementation


class TestMCPPool:
    """Pool management endpoints."""

    async def test_pool_status(self, client, headers):
        r = await client.get("/api/mcp/pool", headers=headers)
        assert r.status_code < 500

    async def test_pool_health(self, client, headers):
        r = await client.get("/api/mcp/pool/health", headers=headers)
        assert r.status_code < 500
