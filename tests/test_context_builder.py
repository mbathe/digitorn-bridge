"""Tests for the context_builder module - Tool Discovery Engine.

Covers:
- Tokenizer and scoring engine
- Tool schema conversion
- Index building with policy filtering
- All 5 module actions (search, get, execute, list, browse)
- System prompt assembly
- Edge cases (empty index, unknown tools, pagination)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from digitorn.modules.context_builder.builder import build_index
from digitorn.modules.context_builder.module import ContextBuilderModule
from digitorn.modules.context_builder.params import (
    BrowseCategoryParams,
    ExecuteToolParams,
    GetToolParams,
    ListCategoriesParams,
    SearchToolsParams,
)
from digitorn.modules.context_builder.prompt import build_system_prompt
from digitorn.modules.context_builder.scoring import (
    _build_params_summary,
    search,
    tokenize,
    tokenize_for_index,
)
from digitorn.modules.context_builder.tool_schema import action_entry_to_json_schema
from digitorn.modules.context_builder.types import (
    CategoryInfo,
    Decision,
    IndexedTool,
    ScoredTool,
    ToolIndex,
)


# ---------------------------------------------------------------------------
# Helpers - fake modules with realistic action registries
# ---------------------------------------------------------------------------


@dataclass
class FakeParamSpec:
    name: str
    type: str
    description: str
    required: bool = True
    default: Any = None
    enum: list[Any] | None = None
    example: Any = None


@dataclass
class FakeActionSpec:
    name: str
    description: str
    risk_level: str = "low"
    tags: list[str] = field(default_factory=list)
    examples: list[dict[str, Any]] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    irreversible: bool = False
    side_effects: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] | None = None
    params: list[FakeParamSpec] = field(default_factory=list)
    execution_mode: str = "async"
    require_approval: bool = False


@dataclass
class FakeActionEntry:
    name: str
    spec: FakeActionSpec
    params_model: type | None = None
    handler: Any = None


def _make_params_model(name: str, fields: dict[str, tuple[str, str]]):
    """Create a simple Pydantic model dynamically for testing."""
    from pydantic import BaseModel, Field

    annotations = {}
    namespace = {"__annotations__": annotations}
    for fname, (ftype, fdesc) in fields.items():
        type_map = {"str": str, "int": int, "float": float, "bool": bool}
        annotations[fname] = type_map.get(ftype, str)
        namespace[fname] = Field(default=..., description=fdesc)

    model = type(name, (BaseModel,), namespace)
    return model


def _make_module(
    module_id: str,
    actions: dict[str, dict[str, Any]],
    description: str = "",
) -> MagicMock:
    """Create a mock module with a realistic _action_registry.

    ``actions`` maps action_name → {description, risk_level, tags, params_model?, ...}
    """
    module = MagicMock()
    module.MODULE_ID = module_id

    registry = {}
    for action_name, spec_kwargs in actions.items():
        params_model = spec_kwargs.pop("params_model", None)
        spec = FakeActionSpec(name=action_name, **spec_kwargs)
        entry = FakeActionEntry(
            name=action_name,
            spec=spec,
            params_model=params_model,
        )
        registry[action_name] = entry

    module._action_registry = registry

    # Mock manifest
    manifest = MagicMock()
    manifest.description = description or f"Module {module_id}"
    module.get_manifest.return_value = manifest

    # Mock execute
    module.execute = AsyncMock()

    return module


def _make_security_profile(
    *,
    default_policy: str = "auto",
    max_risk_level: str = "high",
    module_grants: dict | None = None,
    hidden_modules: list[str] | None = None,
    blocked_actions: dict[str, list[str]] | None = None,
):
    """Create a fake SecurityProfile for testing."""
    from digitorn.core.security import ModuleGrant, SecurityProfile

    grants = {}
    for mid in (hidden_modules or []):
        grants[mid] = ModuleGrant(module_id=mid, visibility="hidden")
    for mid, actions in (blocked_actions or {}).items():
        overrides = {a: "block" for a in actions}
        existing = grants.get(mid)
        if existing:
            merged = dict(existing.action_overrides)
            merged.update(overrides)
            grants[mid] = ModuleGrant(
                module_id=mid,
                visibility=existing.visibility,
                action_overrides=merged,
            )
        else:
            grants[mid] = ModuleGrant(module_id=mid, action_overrides=overrides)

    if module_grants:
        grants.update(module_grants)

    return SecurityProfile(
        app_id="test-app",
        default_policy=default_policy,
        max_risk_level=max_risk_level,
        module_grants=grants,
    )


# ===========================================================================
# Tests - Tokenizer
# ===========================================================================


class TestTokenizer:

    def test_basic_tokenize(self):
        tokens = tokenize("Read a file from disk")
        assert "read" in tokens
        assert "file" in tokens
        assert "disk" in tokens
        # Stop words removed
        assert "a" not in tokens
        assert "from" not in tokens

    def test_underscore_split(self):
        tokens = tokenize("fetch_results")
        assert "fetch" in tokens
        assert "results" in tokens

    def test_dot_split(self):
        tokens = tokenize("database.fetch_results")
        assert "database" in tokens
        assert "fetch" in tokens
        assert "results" in tokens

    def test_accent_normalization(self):
        tokens = tokenize("exécuter une requête SQL")
        assert "executer" in tokens
        assert "requete" in tokens
        assert "sql" in tokens

    def test_deduplication(self):
        tokens = tokenize("file file file")
        assert tokens.count("file") == 1

    def test_short_tokens_removed(self):
        tokens = tokenize("a b c de fg")
        assert "de" not in tokens  # stop word in French
        assert "fg" in tokens

    def test_empty_input(self):
        assert tokenize("") == []
        assert tokenize("   ") == []

    def test_tokenize_for_index_keeps_stop_words(self):
        tokens = tokenize_for_index("Read a file")
        # tokenize_for_index doesn't remove stop words
        assert "read" in tokens
        assert "file" in tokens


# ===========================================================================
# Tests - Tool Schema Conversion
# ===========================================================================


class TestToolSchema:

    def test_pydantic_model_schema(self):
        model = _make_params_model("TestParams", {
            "query": ("str", "The SQL query"),
            "limit": ("int", "Max rows"),
        })
        entry = FakeActionEntry(
            name="test",
            spec=FakeActionSpec(name="test", description="test"),
            params_model=model,
        )
        schema = action_entry_to_json_schema(entry)
        assert schema["type"] == "object"
        assert "query" in schema["properties"]
        assert "limit" in schema["properties"]

    def test_input_schema_fallback(self):
        spec = FakeActionSpec(
            name="test",
            description="test",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        )
        entry = FakeActionEntry(name="test", spec=spec)
        schema = action_entry_to_json_schema(entry)
        assert schema["properties"]["x"]["type"] == "string"

    def test_param_spec_fallback(self):
        spec = FakeActionSpec(
            name="test",
            description="test",
            params=[
                FakeParamSpec(name="path", type="string", description="File path"),
                FakeParamSpec(name="verbose", type="boolean", description="Verbose", required=False),
            ],
        )
        entry = FakeActionEntry(name="test", spec=spec)
        schema = action_entry_to_json_schema(entry)
        assert "path" in schema["properties"]
        assert "verbose" in schema["properties"]
        assert "path" in schema["required"]
        assert "verbose" not in schema.get("required", [])

    def test_no_params(self):
        spec = FakeActionSpec(name="test", description="test")
        entry = FakeActionEntry(name="test", spec=spec)
        schema = action_entry_to_json_schema(entry)
        assert schema == {"type": "object", "properties": {}, "required": []}


# ===========================================================================
# Tests - Params Summary
# ===========================================================================


class TestParamsSummary:

    def test_basic_summary(self):
        schema = {
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        }
        summary = _build_params_summary(schema)
        assert "query: str" in summary
        assert "limit?: int" in summary

    def test_empty_schema(self):
        assert _build_params_summary({}) == "(none)"
        assert _build_params_summary({"properties": {}}) == "(none)"


# ===========================================================================
# Tests - Index Builder
# ===========================================================================


class TestIndexBuilder:

    def test_build_basic_index(self):
        modules = {
            "database": _make_module("database", {
                "fetch_results": {
                    "description": "Execute a read-only SQL query",
                    "risk_level": "low",
                    "tags": ["query", "read-only"],
                },
                "execute_query": {
                    "description": "Execute an arbitrary SQL statement",
                    "risk_level": "high",
                    "tags": ["query", "write"],
                },
            }, description="Database operations"),
        }

        index = build_index(modules)

        assert index.total_tools == 2
        assert index.total_categories == 1
        assert "database.fetch_results" in index.tools
        assert "database.execute_query" in index.tools
        assert "database" in index.categories
        assert index.categories["database"].tool_count == 2

    def test_multiple_modules(self):
        modules = {
            "database": _make_module("database", {
                "fetch": {"description": "Fetch data"},
            }),
            "filesystem": _make_module("filesystem", {
                "read": {"description": "Read a file"},
                "write": {"description": "Write a file"},
            }),
        }

        index = build_index(modules)

        assert index.total_tools == 3
        assert index.total_categories == 2
        assert "database" in index.categories
        assert "filesystem" in index.categories

    def test_context_builder_hidden(self):
        """context_builder itself is never indexed."""
        modules = {
            "context_builder": _make_module("context_builder", {
                "search_tools": {"description": "Search tools"},
            }),
            "database": _make_module("database", {
                "fetch": {"description": "Fetch data"},
            }),
        }

        index = build_index(modules)

        assert "context_builder.search_tools" not in index.tools
        assert "context_builder" not in index.categories
        assert index.total_tools == 1

    def test_security_hides_module(self):
        modules = {
            "database": _make_module("database", {
                "fetch": {"description": "Fetch data"},
            }),
            "secret_module": _make_module("secret_module", {
                "secret_action": {"description": "Top secret"},
            }),
        }
        profile = _make_security_profile(hidden_modules=["secret_module"])

        index = build_index(modules, profile)

        assert index.total_tools == 1
        assert "secret_module.secret_action" not in index.tools
        assert "secret_module" not in index.categories

    def test_security_blocks_action(self):
        modules = {
            "database": _make_module("database", {
                "fetch": {"description": "Fetch data", "risk_level": "low"},
                "drop_table": {"description": "Drop a table", "risk_level": "high"},
            }),
        }
        profile = _make_security_profile(
            blocked_actions={"database": ["drop_table"]},
        )

        index = build_index(modules, profile)

        assert "database.fetch" in index.tools
        assert "database.drop_table" not in index.tools
        assert index.categories["database"].tool_count == 1

    def test_keyword_index_populated(self):
        modules = {
            "database": _make_module("database", {
                "fetch_results": {
                    "description": "Execute a read-only SQL query",
                    "tags": ["query", "sql"],
                },
            }),
        }

        index = build_index(modules)

        # "fetch" should be in keyword index
        assert "fetch" in index.keyword_index
        assert "database.fetch_results" in index.keyword_index["fetch"]
        # "sql" should be in both keyword and tag index
        assert "sql" in index.keyword_index
        assert "sql" in index.tag_index

    def test_empty_modules(self):
        index = build_index({})
        assert index.total_tools == 0
        assert index.total_categories == 0

    def test_module_with_no_actions(self):
        module = MagicMock()
        module.MODULE_ID = "empty"
        module._action_registry = {}
        manifest = MagicMock()
        manifest.description = "Empty module"
        module.get_manifest.return_value = manifest

        index = build_index({"empty": module})
        assert index.total_tools == 0
        assert "empty" not in index.categories


# ===========================================================================
# Tests - Search Engine
# ===========================================================================


class TestSearch:

    @pytest.fixture()
    def rich_index(self):
        """An index with diverse tools for search testing."""
        modules = {
            "database": _make_module("database", {
                "fetch_results": {
                    "description": "Execute a read-only SQL query and return results",
                    "risk_level": "low",
                    "tags": ["query", "sql", "read-only"],
                },
                "execute_query": {
                    "description": "Execute an arbitrary SQL statement with write access",
                    "risk_level": "high",
                    "tags": ["query", "sql", "write"],
                },
                "list_tables": {
                    "description": "List all tables in the connected database",
                    "risk_level": "low",
                    "tags": ["schema", "info"],
                },
            }, description="Database operations: connections, queries, schema"),
            "filesystem": _make_module("filesystem", {
                "read_file": {
                    "description": "Read the text content of a file from disk",
                    "risk_level": "low",
                    "tags": ["io", "read"],
                },
                "write_file": {
                    "description": "Write text content to a file on disk",
                    "risk_level": "medium",
                    "tags": ["io", "write"],
                },
                "delete_file": {
                    "description": "Delete a file from the filesystem",
                    "risk_level": "high",
                    "tags": ["io", "delete", "destructive"],
                },
            }, description="File and directory operations"),
            "browser": _make_module("browser", {
                "take_screenshot": {
                    "description": "Capture a screenshot of the current browser page",
                    "risk_level": "low",
                    "tags": ["capture", "visual"],
                },
                "navigate": {
                    "description": "Navigate the browser to a URL",
                    "risk_level": "medium",
                    "tags": ["navigation"],
                },
            }, description="Web browser automation and scraping"),
        }
        return build_index(modules)

    def test_search_sql_query(self, rich_index):
        results = search(rich_index, "SQL query")
        assert len(results) > 0
        # Top result should be database-related
        fqns = [r.fqn for r in results]
        assert any("database" in f for f in fqns)

    def test_search_read_file(self, rich_index):
        results = search(rich_index, "read a file")
        assert len(results) > 0
        fqns = [r.fqn for r in results]
        assert "filesystem.read_file" in fqns

    def test_search_screenshot(self, rich_index):
        results = search(rich_index, "take a screenshot")
        assert len(results) > 0
        assert results[0].fqn == "browser.take_screenshot"

    def test_search_by_module_name(self, rich_index):
        results = search(rich_index, "database")
        assert len(results) > 0
        # All database tools should appear
        fqns = [r.fqn for r in results]
        assert any(f.startswith("database.") for f in fqns)

    def test_search_by_tag(self, rich_index):
        results = search(rich_index, "destructive")
        assert len(results) > 0
        assert "filesystem.delete_file" in [r.fqn for r in results]

    def test_search_by_fqn_substring(self, rich_index):
        results = search(rich_index, "fetch_results")
        assert len(results) > 0
        assert results[0].fqn == "database.fetch_results"

    def test_search_max_results(self, rich_index):
        results = search(rich_index, "file", max_results=2)
        assert len(results) <= 2

    def test_search_empty_query(self, rich_index):
        results = search(rich_index, "")
        assert results == []

    def test_search_no_match(self, rich_index):
        results = search(rich_index, "xyznonexistent12345")
        assert results == []

    def test_search_relevance_ordering(self, rich_index):
        results = search(rich_index, "SQL query database")
        if len(results) >= 2:
            assert results[0].relevance >= results[1].relevance

    def test_search_returns_scored_tools(self, rich_index):
        results = search(rich_index, "file")
        assert all(isinstance(r, ScoredTool) for r in results)
        for r in results:
            assert r.fqn
            assert r.description
            assert r.relevance > 0


# ===========================================================================
# Tests - Module Actions
# ===========================================================================


class TestContextBuilderModule:

    @pytest.fixture()
    def module_with_index(self):
        """A ContextBuilderModule with a populated index."""
        modules = {
            "database": _make_module("database", {
                "fetch_results": {
                    "description": "Execute a read-only SQL query",
                    "risk_level": "low",
                    "tags": ["query", "sql"],
                },
                "execute_query": {
                    "description": "Execute SQL with write access",
                    "risk_level": "high",
                    "tags": ["query", "sql", "write"],
                },
            }, description="Database operations"),
            "filesystem": _make_module("filesystem", {
                "read_file": {
                    "description": "Read file content",
                    "risk_level": "low",
                    "tags": ["io", "read"],
                },
            }, description="File operations"),
        }

        cb = ContextBuilderModule()
        cb.build_and_set_index(modules)
        # Store modules for execute_tool tests
        cb._test_modules = modules
        return cb

    # --- search_tools ---

    @pytest.mark.asyncio()
    async def test_search_tools(self, module_with_index):
        result = await module_with_index.search_tools(
            SearchToolsParams(query="SQL query")
        )
        assert result.success
        assert result.data["count"] > 0
        assert result.data["query"] == "SQL query"
        tools = result.data["tools"]
        assert all("name" in t for t in tools)

    @pytest.mark.asyncio()
    async def test_search_tools_empty(self, module_with_index):
        result = await module_with_index.search_tools(
            SearchToolsParams(query="xyznonexistent12345")
        )
        assert result.success
        assert result.data["count"] == 0

    # --- get_tool ---

    @pytest.mark.asyncio()
    async def test_get_tool_found(self, module_with_index):
        result = await module_with_index.get_tool(
            GetToolParams(name="database.fetch_results")
        )
        assert result.success
        assert result.data["name"] == "database.fetch_results"
        assert result.data["module"] == "database"
        assert "parameters" in result.data
        assert result.data["risk_level"] == "low"

    @pytest.mark.asyncio()
    async def test_get_tool_not_found(self, module_with_index):
        result = await module_with_index.get_tool(
            GetToolParams(name="nonexistent.action")
        )
        assert not result.success
        assert "not found" in result.error

    # --- execute_tool ---

    @pytest.mark.asyncio()
    async def test_execute_tool_success(self, module_with_index):
        from digitorn.modules.base import ActionResult

        db_module = module_with_index._test_modules["database"]
        db_module.execute.return_value = ActionResult(
            success=True, data={"rows": [{"id": 1}]}
        )

        result = await module_with_index.execute_tool(
            ExecuteToolParams(
                name="database.fetch_results",
                params={"query": "SELECT 1"},
            )
        )
        assert result.success
        assert result.data["rows"] == [{"id": 1}]
        db_module.execute.assert_awaited_once_with("fetch_results", {"query": "SELECT 1"}, context=None)

    @pytest.mark.asyncio()
    async def test_execute_tool_not_found(self, module_with_index):
        result = await module_with_index.execute_tool(
            ExecuteToolParams(name="nonexistent.action", params={})
        )
        assert not result.success
        assert "not found" in result.error

    @pytest.mark.asyncio()
    async def test_execute_tool_exception(self, module_with_index):
        db_module = module_with_index._test_modules["database"]
        db_module.execute.side_effect = RuntimeError("Connection lost")

        result = await module_with_index.execute_tool(
            ExecuteToolParams(
                name="database.fetch_results",
                params={"query": "SELECT 1"},
            )
        )
        assert not result.success
        assert "Connection lost" in result.error

    @pytest.mark.asyncio()
    async def test_execute_tool_approve_required(self):
        """Tools with APPROVE policy require _approved flag."""
        from digitorn.core.security import ModuleGrant

        modules = {
            "database": _make_module("database", {
                "drop_table": {
                    "description": "Drop a table",
                    "risk_level": "medium",
                    "tags": ["destructive"],
                },
            }),
        }
        # Explicitly set action to "approve" via override
        profile = _make_security_profile(
            default_policy="auto",
            max_risk_level="high",
            module_grants={
                "database": ModuleGrant(
                    module_id="database",
                    action_overrides={"drop_table": "approve"},
                ),
            },
        )

        cb = ContextBuilderModule()
        cb.build_and_set_index(modules, profile)

        result = await cb.execute_tool(
            ExecuteToolParams(name="database.drop_table", params={})
        )
        assert not result.success
        assert "approval" in result.error.lower()
        assert result.metadata["requires_approval"] is True

    # --- list_categories ---

    @pytest.mark.asyncio()
    async def test_list_categories(self, module_with_index):
        result = await module_with_index.list_categories(ListCategoriesParams())
        assert result.success
        assert result.data["total_categories"] == 2
        assert result.data["total_tools"] == 3
        ids = [c["id"] for c in result.data["categories"]]
        assert "database" in ids
        assert "filesystem" in ids

    # --- browse_category ---

    @pytest.mark.asyncio()
    async def test_browse_category(self, module_with_index):
        result = await module_with_index.browse_category(
            BrowseCategoryParams(category="database")
        )
        assert result.success
        assert result.data["category"] == "database"
        assert result.data["total_tools"] == 2
        assert len(result.data["tools"]) == 2

    @pytest.mark.asyncio()
    async def test_browse_category_not_found(self, module_with_index):
        result = await module_with_index.browse_category(
            BrowseCategoryParams(category="nonexistent")
        )
        assert not result.success
        assert "not found" in result.error

    @pytest.mark.asyncio()
    async def test_browse_category_pagination(self):
        """Pagination works with many tools."""
        # Create a module with 25 actions (exceeds page size of 20)
        actions = {}
        for i in range(25):
            actions[f"action_{i:02d}"] = {
                "description": f"Action number {i}",
                "risk_level": "low",
            }
        modules = {"big_module": _make_module("big_module", actions)}

        cb = ContextBuilderModule()
        cb.build_and_set_index(modules)

        # Page 1
        result1 = await cb.browse_category(
            BrowseCategoryParams(category="big_module", page=1)
        )
        assert result1.success
        assert len(result1.data["tools"]) == 20
        assert result1.data["total_pages"] == 2

        # Page 2
        result2 = await cb.browse_category(
            BrowseCategoryParams(category="big_module", page=2)
        )
        assert result2.success
        assert len(result2.data["tools"]) == 5

    # --- Lifecycle ---

    @pytest.mark.asyncio()
    async def test_on_stop_clears_index(self, module_with_index):
        assert module_with_index.index.total_tools > 0
        await module_with_index.on_stop()
        assert module_with_index.index.total_tools == 0

    def test_set_index(self):
        cb = ContextBuilderModule()
        custom_index = ToolIndex()
        custom_index.tools["test.action"] = IndexedTool(
            fqn="test.action",
            module_id="test",
            action_name="action",
            description="A test action",
            risk_level="low",
            tags=[],
            params_schema={},
            examples=[],
            module=MagicMock(),
            policy_decision=Decision.AUTO,
        )
        cb.set_index(custom_index)
        assert cb.index.total_tools == 1

    def test_manifest(self):
        cb = ContextBuilderModule()
        manifest = cb.get_manifest()
        assert manifest.module_id == "context_builder"
        assert "Discovery" in manifest.description


# ===========================================================================
# Tests - System Prompt Assembly
# ===========================================================================


class TestPromptAssembly:

    def test_basic_prompt(self):
        index = ToolIndex()
        # Add some fake tools and categories
        tool_a = MagicMock()
        tool_a.description = "Tool A description."
        tool_a.action_name = "b"
        tool_a.tags = ["test"]
        tool_a.examples = []
        tool_b = MagicMock()
        tool_b.description = "Tool B description."
        tool_b.action_name = "d"
        tool_b.tags = ["test"]
        tool_b.examples = []
        index.tools["a.b"] = tool_a
        index.tools["c.d"] = tool_b
        cat_a = MagicMock()
        cat_a.tool_count = 1
        cat_a.summary = "Category A tools"
        cat_a.tool_names = ["a.b"]
        cat_b = MagicMock()
        cat_b.tool_count = 1
        cat_b.summary = "Category C tools"
        cat_b.tool_names = ["c.d"]
        index.categories["a"] = cat_a
        index.categories["c"] = cat_b

        prompt = build_system_prompt(
            agent_id="coordinator",
            role="coordinator",
            user_prompt="You are a helpful coordinator.",
            index=index,
        )

        assert 'agent "coordinator"' in prompt
        assert "role: coordinator" in prompt
        assert "2 tools" in prompt
        assert "2 domains" in prompt
        assert "You are a helpful coordinator." in prompt

    def test_empty_user_prompt(self):
        index = ToolIndex()
        prompt = build_system_prompt(
            agent_id="worker",
            role="worker",
            user_prompt="",
            index=index,
        )
        assert 'agent "worker"' in prompt
        assert "search_tools" in prompt

    def test_counts_reflect_index(self):
        index = ToolIndex()
        for i in range(100):
            t = MagicMock()
            t.description = f"Action {i} description."
            t.action_name = f"action_{i}"
            t.tags = []
            t.examples = []
            index.tools[f"mod.action_{i}"] = t
        for i in range(10):
            c = MagicMock()
            c.tool_count = 10
            c.summary = f"Module {i} tools"
            c.tool_names = [f"mod.action_{j}" for j in range(i * 10, (i + 1) * 10)]
            index.categories[f"mod_{i}"] = c

        prompt = build_system_prompt("a", "worker", "", index)
        assert "100 tools" in prompt
        assert "10 domains" in prompt


# ===========================================================================
# Tests - Security Integration
# ===========================================================================


class TestSecurityIntegration:

    def test_blocked_tools_invisible_to_search(self):
        modules = {
            "database": _make_module("database", {
                "fetch": {"description": "Safe fetch", "risk_level": "low"},
                "drop_all": {"description": "Drop everything", "risk_level": "high"},
            }),
        }
        profile = _make_security_profile(
            blocked_actions={"database": ["drop_all"]},
        )

        index = build_index(modules, profile)

        # Blocked tool not in index
        assert "database.drop_all" not in index.tools

        # Search cannot find it
        results = search(index, "drop everything")
        fqns = [r.fqn for r in results]
        assert "database.drop_all" not in fqns

    def test_hidden_module_invisible(self):
        modules = {
            "public": _make_module("public", {
                "action": {"description": "Public action"},
            }),
            "secret": _make_module("secret", {
                "action": {"description": "Secret action"},
            }),
        }
        profile = _make_security_profile(hidden_modules=["secret"])

        index = build_index(modules, profile)

        assert "secret.action" not in index.tools
        assert "secret" not in index.categories
        results = search(index, "secret")
        assert all(r.fqn != "secret.action" for r in results)

    def test_approve_policy_preserved(self):
        modules = {
            "database": _make_module("database", {
                "write": {"description": "Write data", "risk_level": "medium"},
            }),
        }
        profile = _make_security_profile(default_policy="approve")

        index = build_index(modules, profile)

        tool = index.tools["database.write"]
        assert tool.policy_decision == Decision.APPROVE

    def test_no_profile_all_auto(self):
        modules = {
            "database": _make_module("database", {
                "action": {"description": "An action", "risk_level": "high"},
            }),
        }

        index = build_index(modules, security_profile=None)

        tool = index.tools["database.action"]
        assert tool.policy_decision == Decision.AUTO


# ===========================================================================
# Tests - Edge Cases
# ===========================================================================


class TestEdgeCases:

    def test_empty_index_operations(self):
        cb = ContextBuilderModule()
        assert cb.index.total_tools == 0
        assert cb.index.total_categories == 0

    @pytest.mark.asyncio()
    async def test_search_on_empty_index(self):
        cb = ContextBuilderModule()
        result = await cb.search_tools(SearchToolsParams(query="anything"))
        assert result.success
        assert result.data["count"] == 0

    @pytest.mark.asyncio()
    async def test_list_categories_empty(self):
        cb = ContextBuilderModule()
        result = await cb.list_categories(ListCategoriesParams())
        assert result.success
        assert result.data["total_categories"] == 0

    def test_module_with_failing_manifest(self):
        """Module whose get_manifest() raises still gets indexed."""
        module = MagicMock()
        module.MODULE_ID = "broken"
        module._action_registry = {
            "action": FakeActionEntry(
                name="action",
                spec=FakeActionSpec(name="action", description="Works fine"),
            ),
        }
        module.get_manifest.side_effect = RuntimeError("Manifest broken")

        index = build_index({"broken": module})

        # Still indexed, with fallback description
        assert "broken.action" in index.tools
        assert "broken" in index.categories
        assert "Module: broken" in index.categories["broken"].summary

    def test_special_characters_in_query(self):
        """Search handles special characters gracefully."""
        modules = {
            "mod": _make_module("mod", {
                "action": {"description": "Normal action"},
            }),
        }
        index = build_index(modules)

        # Should not crash
        results = search(index, "!@#$%^&*()")
        assert isinstance(results, list)

    def test_unicode_in_description(self):
        """Tools with unicode descriptions are indexed correctly."""
        modules = {
            "mod": _make_module("mod", {
                "action": {"description": "Exécuter une requête spéciale"},
            }),
        }
        index = build_index(modules)

        results = search(index, "requête spéciale")
        assert len(results) > 0

    @pytest.mark.asyncio()
    async def test_execute_tool_wraps_non_actionresult(self):
        """execute_tool wraps raw return values in ActionResult."""
        modules = {
            "mod": _make_module("mod", {
                "action": {"description": "Returns raw dict"},
            }),
        }
        modules["mod"].execute.return_value = {"raw": "data"}

        cb = ContextBuilderModule()
        cb.build_and_set_index(modules)

        result = await cb.execute_tool(
            ExecuteToolParams(name="mod.action", params={})
        )
        assert result.success
        assert result.data == {"raw": "data"}
