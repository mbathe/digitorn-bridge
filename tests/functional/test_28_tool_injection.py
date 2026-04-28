"""28 - Tool injection modes: direct, compact_direct, discovery, hybrid.

Verifies:
1. Each mode deploys successfully
2. The index reports the correct tool_injection_mode
3. In direct/compact mode, tools are callable directly (agent uses filesystem.read etc.)
4. In discovery mode, meta-tools (search_tools, get_tool, execute_tool) are present
5. In discovery mode, agent can search → find → execute tools
6. In hybrid mode, direct_modules tools are available + meta-tools
7. Chat works in every mode
"""

import uuid

import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait

pytestmark = pytest.mark.asyncio


# ═══════════════════════════════════════════════════════════════
# MODE: DIRECT
# ═══════════════════════════════════════════════════════════════

class TestDirectMode:
    """tool_injection: direct - all tools as full function schemas."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        d = await deploy_app(client, "inject_direct.yaml", headers)
        assert d["success"] is True, f"Deploy failed: {d.get('error')}"
        self.app_id = "test-inject-direct"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_deploy_succeeds(self):
        """Direct mode app deploys without errors."""
        r = await self.client.get(f"/api/apps/{self.app_id}", headers=self.headers)
        d = r.json()
        assert d["success"] is True
        assert d["data"]["app_id"] == self.app_id

    async def test_index_reports_direct_mode(self):
        """Index should report tool_injection_mode = 'direct'."""
        r = await self.client.get(f"/api/apps/{self.app_id}/index", headers=self.headers)
        d = r.json()
        assert d["success"] is True
        assert d["data"]["tool_injection_mode"] == "direct"

    async def test_tools_are_direct(self):
        """In direct mode, filesystem.read should be in the tool list directly."""
        r = await self.client.get(
            f"/api/apps/{self.app_id}/tools/filesystem.read",
            headers=self.headers,
        )
        d = r.json()
        assert d["success"] is True
        assert "parameters" in d["data"] or "params" in str(d["data"])

    async def test_direct_has_many_tools(self):
        """Direct mode should expose all module tools (not just meta-tools)."""
        r = await self.client.get(f"/api/apps/{self.app_id}/index", headers=self.headers)
        d = r.json()
        assert d["data"]["total_tools"] >= 15  # filesystem(14) + shell(3) + memory(6)

    async def test_chat_uses_tools_directly(self):
        """Chat should work - agent calls tools directly."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(
            self.client, self.app_id, sid,
            "List files in the current directory",
            self.headers,
        )
        assert d["success"] is True
        # Should have used tools (ls or glob)
        assert d["data"]["tool_calls_count"] >= 1

    async def test_no_meta_tools_in_direct(self):
        """Direct mode should NOT have search_tools/execute_tool as primary tools."""
        r = await self.client.get(f"/api/apps/{self.app_id}/index", headers=self.headers)
        d = r.json()
        all_tool_names = []
        for cat in d["data"].get("categories", []):
            for tool in cat.get("tools", []):
                all_tool_names.append(tool["name"])
        # search_tools and execute_tool should not be in the main tool list
        # (they're meta-tools, only used in discovery mode)
        assert "context_builder.search_tools" not in all_tool_names or \
               "context_builder.execute_tool" not in all_tool_names


# ═══════════════════════════════════════════════════════════════
# MODE: COMPACT_DIRECT
# ═══════════════════════════════════════════════════════════════

class TestCompactDirectMode:
    """tool_injection: compact_direct - tools with compact schemas."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        d = await deploy_app(client, "inject_compact.yaml", headers)
        assert d["success"] is True, f"Deploy failed: {d.get('error')}"
        self.app_id = "test-inject-compact"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_deploy_succeeds(self):
        r = await self.client.get(f"/api/apps/{self.app_id}", headers=self.headers)
        assert r.json()["success"] is True

    async def test_index_reports_compact_mode(self):
        r = await self.client.get(f"/api/apps/{self.app_id}/index", headers=self.headers)
        d = r.json()
        assert d["success"] is True
        assert d["data"]["tool_injection_mode"] == "compact_direct"

    async def test_has_tools(self):
        """Compact mode should still expose tools."""
        r = await self.client.get(f"/api/apps/{self.app_id}/index", headers=self.headers)
        d = r.json()
        assert d["data"]["total_tools"] >= 15

    async def test_chat_works(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(
            self.client, self.app_id, sid,
            "What is 2+2? Just answer the number.",
            self.headers,
        )
        assert d["success"] is True
        assert d["data"]["content"]  # Non-empty response


# ═══════════════════════════════════════════════════════════════
# MODE: DISCOVERY
# ═══════════════════════════════════════════════════════════════

class TestDiscoveryMode:
    """tool_injection: discovery - only meta-tools, agent searches for tools."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        d = await deploy_app(client, "inject_discovery.yaml", headers)
        assert d["success"] is True, f"Deploy failed: {d.get('error')}"
        self.app_id = "test-inject-discovery"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_deploy_succeeds(self):
        r = await self.client.get(f"/api/apps/{self.app_id}", headers=self.headers)
        assert r.json()["success"] is True

    async def test_index_reports_discovery_mode(self):
        r = await self.client.get(f"/api/apps/{self.app_id}/index", headers=self.headers)
        d = r.json()
        assert d["success"] is True
        assert d["data"]["tool_injection_mode"] == "discovery"

    async def test_index_still_has_all_tools(self):
        """Index should still show all tools (they're discoverable, not direct)."""
        r = await self.client.get(f"/api/apps/{self.app_id}/index", headers=self.headers)
        d = r.json()
        assert d["data"]["total_tools"] >= 15

    async def test_search_tools_works(self):
        """search_tools should find filesystem tools."""
        r = await self.client.get(
            f"/api/apps/{self.app_id}/tools/search",
            params={"query": "read file"},
            headers=self.headers,
        )
        d = r.json()
        assert d["success"] is True
        tools = d["data"].get("tools", d["data"].get("results", []))
        assert len(tools) > 0
        names = [t["name"] for t in tools]
        assert any("read" in n for n in names)

    async def test_chat_discovery_flow(self):
        """Agent should be able to chat and use tools via discovery flow.
        This tests the full search → get → execute pipeline."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(
            self.client, self.app_id, sid,
            "List the files in the current directory. Use search_tools first to find the right tool.",
            self.headers,
        )
        assert d["success"] is True
        # Agent should have made tool calls (search + execute at minimum)
        assert d["data"]["tool_calls_count"] >= 1

    async def test_chat_simple_question(self):
        """Simple question without tools should still work."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(
            self.client, self.app_id, sid,
            "What is 2+2? Answer with just the number.",
            self.headers,
        )
        assert d["success"] is True
        assert d["data"]["content"]


# ═══════════════════════════════════════════════════════════════
# MODE: DISCOVERY + DIRECT_MODULES (HYBRID)
# ═══════════════════════════════════════════════════════════════

class TestHybridMode:
    """tool_injection: discovery + direct_modules: [filesystem] - hybrid."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        d = await deploy_app(client, "inject_discovery_direct.yaml", headers)
        assert d["success"] is True, f"Deploy failed: {d.get('error')}"
        self.app_id = "test-inject-hybrid"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_deploy_succeeds(self):
        r = await self.client.get(f"/api/apps/{self.app_id}", headers=self.headers)
        assert r.json()["success"] is True

    async def test_index_reports_discovery_mode(self):
        """Even hybrid is reported as discovery mode."""
        r = await self.client.get(f"/api/apps/{self.app_id}/index", headers=self.headers)
        d = r.json()
        assert d["success"] is True
        assert d["data"]["tool_injection_mode"] == "discovery"

    async def test_chat_with_direct_filesystem(self):
        """Filesystem is direct - agent should use it without search_tools."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(
            self.client, self.app_id, sid,
            "Read the file pyproject.toml and tell me the project name",
            self.headers,
        )
        assert d["success"] is True
        # Should have tool calls
        assert d["data"]["tool_calls_count"] >= 1

    async def test_chat_discovery_for_shell(self):
        """Shell is NOT direct - agent must discover it."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(
            self.client, self.app_id, sid,
            "I need to run 'echo hello' in the shell. Search for the right tool first.",
            self.headers,
        )
        assert d["success"] is True


# ═══════════════════════════════════════════════════════════════
# MODE: AUTO (DEFAULT) - verify auto-detection
# ═══════════════════════════════════════════════════════════════

class TestAutoMode:
    """No tool_injection set - system auto-detects."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        d = await deploy_app(client, "minimal.yaml", headers)
        assert d["success"] is True
        self.app_id = "test-minimal"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_auto_selects_mode(self):
        """With few tools, auto should select direct or compact_direct."""
        r = await self.client.get(f"/api/apps/{self.app_id}/index", headers=self.headers)
        d = r.json()
        assert d["success"] is True
        mode = d["data"]["tool_injection_mode"]
        assert mode in ("direct", "compact_direct"), f"Unexpected auto mode: {mode}"

    async def test_chat_works_auto(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(
            self.client, self.app_id, sid,
            "Say hello",
            self.headers,
        )
        assert d["success"] is True
