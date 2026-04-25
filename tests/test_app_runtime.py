"""Tests for RuntimeApp and AppRuntimeStore.

Verifies that the pre-computed runtime cache is built correctly
and provides O(1) lookups for policies, constraints, and configs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from digitorn.core.app.compiler import CompiledApp, CompiledModuleConfig, CompiledSetupStep
from digitorn.core.app.runtime import (
    AppRuntimeStore,
    RuntimeApp,
    build_runtime_app,
)
from digitorn.core.app.schema import AppMeta
from digitorn.core.security import ModuleGrant, SecurityProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action_spec(name: str, risk_level: str = "low", permissions: list | None = None):
    spec = MagicMock()
    spec.name = name
    spec.risk_level = risk_level
    spec.permissions = permissions or []
    spec.irreversible = False
    return spec


def _make_action_entry(name: str, risk_level: str = "low", permissions: list | None = None):
    entry = MagicMock()
    entry.name = name
    entry.spec = _make_action_spec(name, risk_level, permissions)
    return entry


def _make_module(module_id: str, actions: dict[str, str] | None = None):
    """Create a mock module. actions = {name: risk_level}."""
    module = MagicMock()
    module.MODULE_ID = module_id
    actions = actions or {}
    registry = {}
    for name, risk in actions.items():
        registry[name] = _make_action_entry(name, risk)
    module._action_registry = registry
    return module


def _make_registry(modules: dict[str, MagicMock]):
    registry = MagicMock()
    registry.list_available.return_value = list(modules.keys())

    def get(mid):
        if mid not in modules:
            raise Exception(f"Module '{mid}' not found")
        return modules[mid]

    registry.get = MagicMock(side_effect=get)
    return registry


def _make_compiled(
    app_id: str = "test-app",
    modules: dict | None = None,
    security_profile: SecurityProfile | None = None,
) -> CompiledApp:
    meta = AppMeta(app_id=app_id, name="Test App")
    if modules is None:
        modules = {
            "database": CompiledModuleConfig(
                module_id="database",
                config={"pool_size": 5},
                constraints={"allowed_actions": ["fetch_results"], "max_rows": 1000},
            ),
        }
    if security_profile is None:
        security_profile = SecurityProfile(
            app_id=app_id,
            default_policy="approve",
            module_grants={
                "database": ModuleGrant(
                    module_id="database",
                    action_overrides={"fetch_results": "auto", "execute_query": "block"},
                ),
            },
        )
    return CompiledApp(meta=meta, modules=modules, security_profile=security_profile)


# ---------------------------------------------------------------------------
# Tests — RuntimeApp
# ---------------------------------------------------------------------------


class TestBuildRuntimeApp:

    def test_pre_resolves_action_policies(self):
        """Action policies are pre-computed for every module:action pair."""
        db_module = _make_module("database", {
            "fetch_results": "low",
            "execute_query": "high",
            "list_tables": "low",
        })
        registry = _make_registry({"database": db_module})
        compiled = _make_compiled()

        runtime = build_runtime_app(compiled, registry)

        assert runtime.action_policy("database", "fetch_results") == "auto"
        assert runtime.action_policy("database", "execute_query") == "block"
        # list_tables has no override → falls through to risk rules
        assert runtime.action_policy("database", "list_tables") in ("auto", "approve")

    def test_stores_constraints(self):
        """Module constraints are accessible via O(1) lookup."""
        db_module = _make_module("database", {"fetch_results": "low"})
        registry = _make_registry({"database": db_module})
        compiled = _make_compiled()

        runtime = build_runtime_app(compiled, registry)

        constraints = runtime.module_constraints("database")
        assert constraints["allowed_actions"] == ["fetch_results"]
        assert constraints["max_rows"] == 1000

    def test_stores_config(self):
        """Module static config is accessible via O(1) lookup."""
        db_module = _make_module("database", {"fetch_results": "low"})
        registry = _make_registry({"database": db_module})
        compiled = _make_compiled()

        runtime = build_runtime_app(compiled, registry)

        config = runtime.module_config("database")
        assert config["pool_size"] == 5

    def test_empty_constraints_returns_empty_dict(self):
        """Missing module returns empty dict, not KeyError."""
        db_module = _make_module("database", {"fetch_results": "low"})
        registry = _make_registry({"database": db_module})
        compiled = _make_compiled()

        runtime = build_runtime_app(compiled, registry)

        assert runtime.module_constraints("unknown") == {}
        assert runtime.module_config("unknown") == {}

    def test_can_execute_check(self):
        """can_execute returns False for blocked actions."""
        db_module = _make_module("database", {
            "fetch_results": "low",
            "execute_query": "high",
        })
        registry = _make_registry({"database": db_module})
        compiled = _make_compiled()

        runtime = build_runtime_app(compiled, registry)

        assert runtime.can_execute("database", "fetch_results") is True
        assert runtime.can_execute("database", "execute_query") is False

    def test_visible_actions(self):
        """Visible actions are pre-computed per module."""
        db_module = _make_module("database", {
            "fetch_results": "low",
            "execute_query": "high",
        })
        registry = _make_registry({"database": db_module})
        compiled = _make_compiled()

        runtime = build_runtime_app(compiled, registry)

        visible = runtime.visible_actions_for("database")
        assert "fetch_results" in visible
        assert "execute_query" in visible  # visible even if blocked

    def test_hidden_module_no_visible_actions(self):
        """Hidden modules have no visible actions."""
        db_module = _make_module("database", {"fetch_results": "low"})
        registry = _make_registry({"database": db_module})

        sp = SecurityProfile(
            app_id="test-app",
            module_grants={
                "database": ModuleGrant(module_id="database", visibility="hidden"),
            },
        )
        compiled = _make_compiled(security_profile=sp)

        runtime = build_runtime_app(compiled, registry)

        assert runtime.visible_actions_for("database") == []
        assert runtime.is_module_visible("database") is False

    def test_action_policy_entry_returns_full_metadata(self):
        """action_policy_entry() returns risk level and permissions."""
        db_module = _make_module("database", {"execute_query": "high"})
        # Set permissions on the spec
        db_module._action_registry["execute_query"].spec.permissions = ["db.write"]
        db_module._action_registry["execute_query"].spec.irreversible = True

        registry = _make_registry({"database": db_module})
        compiled = _make_compiled()

        runtime = build_runtime_app(compiled, registry)

        entry = runtime.action_policy_entry("database", "execute_query")
        assert entry is not None
        assert entry.risk_level == "high"
        assert entry.permissions == ["db.write"]
        assert entry.irreversible is True
        assert entry.policy == "block"

    def test_module_ids_preserved(self):
        """Module IDs are preserved in declaration order."""
        db_module = _make_module("database", {"connect": "low"})
        fs_module = _make_module("filesystem", {"read": "low"})
        registry = _make_registry({"database": db_module, "filesystem": fs_module})

        compiled = _make_compiled(modules={
            "database": CompiledModuleConfig(module_id="database"),
            "filesystem": CompiledModuleConfig(module_id="filesystem"),
        }, security_profile=SecurityProfile(app_id="test-app"))

        runtime = build_runtime_app(compiled, registry)

        assert runtime.module_ids == ("database", "filesystem")

    def test_unknown_action_fallback(self):
        """Unknown action falls back to default_policy."""
        db_module = _make_module("database", {"fetch_results": "low"})
        registry = _make_registry({"database": db_module})

        sp = SecurityProfile(app_id="test-app", default_policy="block")
        compiled = _make_compiled(security_profile=sp)

        runtime = build_runtime_app(compiled, registry)

        # Action not pre-computed → falls back to profile default
        assert runtime.action_policy("database", "nonexistent") == "block"


# ---------------------------------------------------------------------------
# Tests — AppRuntimeStore
# ---------------------------------------------------------------------------


class TestAppRuntimeStore:

    def test_register_and_get(self):
        """Register a compiled app and retrieve it."""
        db_module = _make_module("database", {"fetch_results": "low"})
        registry = _make_registry({"database": db_module})
        compiled = _make_compiled()

        store = AppRuntimeStore(registry)
        runtime = store.register(compiled)

        assert store.get("test-app") is runtime
        assert store.count == 1

    def test_get_nonexistent_returns_none(self):
        registry = _make_registry({})
        store = AppRuntimeStore(registry)

        assert store.get("nonexistent") is None

    def test_register_replaces_existing(self):
        """Re-registering the same app_id replaces the previous entry."""
        db_module = _make_module("database", {"fetch_results": "low"})
        registry = _make_registry({"database": db_module})

        store = AppRuntimeStore(registry)
        r1 = store.register(_make_compiled())
        r2 = store.register(_make_compiled())

        assert store.get("test-app") is r2
        assert store.count == 1

    def test_unregister(self):
        """Unregister removes the app."""
        db_module = _make_module("database", {"fetch_results": "low"})
        registry = _make_registry({"database": db_module})

        store = AppRuntimeStore(registry)
        store.register(_make_compiled())

        assert store.unregister("test-app") is True
        assert store.get("test-app") is None
        assert store.unregister("test-app") is False

    def test_list_apps(self):
        """list_apps returns all registered app IDs."""
        db_module = _make_module("database", {"fetch_results": "low"})
        registry = _make_registry({"database": db_module})

        store = AppRuntimeStore(registry)
        store.register(_make_compiled(app_id="app-1"))
        store.register(_make_compiled(app_id="app-2"))

        assert sorted(store.list_apps()) == ["app-1", "app-2"]
        assert store.count == 2

    def test_multiple_apps_isolated(self):
        """Different apps have independent policies."""
        db_module = _make_module("database", {"fetch_results": "low", "execute_query": "high"})
        registry = _make_registry({"database": db_module})

        store = AppRuntimeStore(registry)

        # App 1: execute_query blocked
        sp1 = SecurityProfile(
            app_id="reader",
            module_grants={"database": ModuleGrant(
                module_id="database",
                action_overrides={"execute_query": "block"},
            )},
        )
        store.register(_make_compiled(app_id="reader", security_profile=sp1))

        # App 2: execute_query auto
        sp2 = SecurityProfile(
            app_id="writer",
            module_grants={"database": ModuleGrant(
                module_id="database",
                action_overrides={"execute_query": "auto"},
            )},
        )
        store.register(_make_compiled(app_id="writer", security_profile=sp2))

        reader = store.get("reader")
        writer = store.get("writer")

        assert reader.action_policy("database", "execute_query") == "block"
        assert writer.action_policy("database", "execute_query") == "auto"
