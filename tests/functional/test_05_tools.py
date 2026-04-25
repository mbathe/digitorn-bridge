"""05 — Tool discovery: search, categories, browse, get schema, direct execute."""

import pytest

from .conftest import deploy_app, undeploy_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def tools_app(client, headers):
    await deploy_app(client, "filesystem_app.yaml", headers)
    yield "test-filesystem"
    await undeploy_app(client, "test-filesystem", headers)


class TestToolSearch:
    """GET /api/apps/{app_id}/tools/search"""

    async def test_search(self, client, headers, tools_app):
        r = await client.get(
            f"/api/apps/{tools_app}/tools/search",
            params={"query": "read file"},
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True
        # data is a dict with "tools" key (list of matching tools)
        tools = d["data"].get("tools", d["data"].get("results", []))
        assert isinstance(tools, list)
        assert len(tools) > 0

    async def test_search_no_results(self, client, headers, tools_app):
        r = await client.get(
            f"/api/apps/{tools_app}/tools/search",
            params={"query": "xyznonexistent98765"},
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True


class TestToolCategories:
    """GET /api/apps/{app_id}/tools/categories"""

    async def test_list_categories(self, client, headers, tools_app):
        r = await client.get(
            f"/api/apps/{tools_app}/tools/categories",
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True
        # data is a dict with "categories" key
        cats = d["data"]["categories"]
        assert isinstance(cats, list)
        assert len(cats) > 0

    async def test_browse_category(self, client, headers, tools_app):
        # Get first category
        r = await client.get(
            f"/api/apps/{tools_app}/tools/categories",
            headers=headers,
        )
        cats = r.json()["data"]["categories"]
        if cats:
            cat_name = cats[0].get("id", cats[0].get("name", ""))
            r2 = await client.get(
                f"/api/apps/{tools_app}/tools/categories/{cat_name}",
                headers=headers,
            )
            assert r2.status_code < 500


class TestToolSchema:
    """GET /api/apps/{app_id}/tools/{tool_name}"""

    async def test_get_tool_schema(self, client, headers, tools_app):
        r = await client.get(
            f"/api/apps/{tools_app}/tools/filesystem.read",
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True
        # Should have schema info
        assert d["data"] is not None

    async def test_get_nonexistent_tool(self, client, headers, tools_app):
        r = await client.get(
            f"/api/apps/{tools_app}/tools/nonexistent.tool",
            headers=headers,
        )
        assert r.status_code == 404 or r.json().get("success") is False


class TestToolIndex:
    """GET /api/apps/{app_id}/index"""

    async def test_full_index(self, client, headers, tools_app):
        r = await client.get(
            f"/api/apps/{tools_app}/index",
            headers=headers,
        )
        d = r.json()
        # If app was just re-deployed, index may not be ready yet
        if r.status_code == 200 and d.get("success") is True:
            assert d["data"] is not None
        else:
            # App may have been undeployed by a previous test — redeploy
            await deploy_app(client, "filesystem_app.yaml", headers)
            r = await client.get(f"/api/apps/{tools_app}/index", headers=headers)
            d = r.json()
            assert d["success"] is True


class TestToolExecute:
    """POST /api/apps/{app_id}/tools/{tool_name}/execute"""

    async def test_execute_tool_directly(self, client, headers, tools_app):
        """Execute filesystem.ls directly."""
        r = await client.post(
            f"/api/apps/{tools_app}/tools/filesystem.ls/execute",
            json={"params": {"path": "."}},
            headers=headers,
        )
        d = r.json()
        # Should succeed or at minimum not crash
        assert r.status_code < 500

    async def test_execute_nonexistent_tool(self, client, headers, tools_app):
        r = await client.post(
            f"/api/apps/{tools_app}/tools/nonexistent.tool/execute",
            json={"params": {}},
            headers=headers,
        )
        assert r.status_code == 404 or r.json().get("success") is False
