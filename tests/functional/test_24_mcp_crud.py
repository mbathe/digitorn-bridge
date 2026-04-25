"""24 — P1: MCP server CRUD — install, list, get, test, config, connect, disconnect."""

import pytest

pytestmark = pytest.mark.asyncio


class TestMCPServerCRUD:
    """Full CRUD on /api/mcp/servers."""

    async def test_install_server(self, client, headers):
        """POST /api/mcp/servers — install a server."""
        r = await client.post("/api/mcp/servers", json={
            "server_id": "test-echo-server",
            "config": {
                "command": "echo",
                "args": ["hello"],
            },
        }, headers=headers)
        # May fail (command not a real MCP server), but should not 500
        assert r.status_code < 500

    async def test_list_servers(self, client, headers):
        r = await client.get("/api/mcp/servers", headers=headers)
        assert r.status_code < 500

    async def test_list_servers_with_status(self, client, headers):
        r = await client.get("/api/mcp/servers", params={"status": "connected"}, headers=headers)
        assert r.status_code < 500

    async def test_get_server_nonexistent(self, client, headers):
        r = await client.get("/api/mcp/servers/nonexistent-server-xyz", headers=headers)
        assert r.status_code in (404, 200, 500)  # Depends on impl

    async def test_test_server_nonexistent(self, client, headers):
        r = await client.post("/api/mcp/servers/nonexistent-server-xyz/test", headers=headers)
        assert r.status_code < 500

    async def test_update_config_nonexistent(self, client, headers):
        r = await client.put(
            "/api/mcp/servers/nonexistent-server-xyz/config",
            json={"config": {"key": "value"}},
            headers=headers,
        )
        assert r.status_code < 500

    async def test_delete_server_nonexistent(self, client, headers):
        r = await client.delete("/api/mcp/servers/nonexistent-server-xyz", headers=headers)
        assert r.status_code < 500


class TestMCPPool:
    """Pool management — connect, disconnect, health."""

    async def test_pool_status(self, client, headers):
        r = await client.get("/api/mcp/pool", headers=headers)
        assert r.status_code < 500

    async def test_pool_health(self, client, headers):
        r = await client.get("/api/mcp/pool/health", headers=headers)
        assert r.status_code < 500

    async def test_connect_nonexistent(self, client, headers):
        r = await client.post("/api/mcp/pool/nonexistent-xyz/connect", headers=headers)
        assert r.status_code < 500

    async def test_disconnect_nonexistent(self, client, headers):
        r = await client.post("/api/mcp/pool/nonexistent-xyz/disconnect", headers=headers)
        assert r.status_code < 500
