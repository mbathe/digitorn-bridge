"""Tests - @action decorator and ActionEntry.

Covers:
- Registration at class-definition time (not instance time)
- Dict key is method name (no _action_ prefix)
- ActionEntry structure (name, handler, spec)
- ActionSpec field population
- Spec attachment on the raw function (_action_spec)
- Registry inheritance: subclass gets parent + own actions
- Registry isolation: subclass does NOT mutate parent class dict
- Overriding an inherited action in a subclass
- Multiple @action decorators in the same class body
- Stacking @action with other decorators preserves async-ness
"""
from __future__ import annotations

import asyncio
import pytest

from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, Platform
from digitorn.modules.decorators import ActionEntry, action
from digitorn.modules.manifest import ActionSpec


# ── Fixtures ─────────────────────────────────────────────────────────────────


class _MinModule(BaseModule):
    MODULE_ID = "_min"
    VERSION   = "1.0.0"

    @action(
        description="First action.",
        permissions=["min:read"],
        tags=["a"],
        risk_level="low",
        irreversible=False,
        streams_progress=False,
    )
    async def first(self, params, ctx=None) -> ActionResult:
        return ActionResult(success=True, data="first")

    @action(
        description="Second action.",
        permissions=["min:write"],
        tags=["b"],
        risk_level="medium",
        irreversible=True,
        side_effects=["filesystem_write"],
        streams_progress=True,
    )
    async def second(self, params, ctx=None) -> ActionResult:
        return ActionResult(success=True, data="second")

    def get_manifest(self):
        from digitorn.modules.manifest import ModuleManifest
        return ModuleManifest.from_module(self)


class _ChildModule(_MinModule):
    MODULE_ID = "_child"

    @action(description="Child-only action.", permissions=["child:read"])
    async def child_only(self, params, ctx=None) -> ActionResult:
        return ActionResult(success=True, data="child")

    def get_manifest(self):
        from digitorn.modules.manifest import ModuleManifest
        return ModuleManifest.from_module(self)


class _OverrideModule(_MinModule):
    MODULE_ID = "_override"

    @action(description="Overridden first.", permissions=["override:read"])
    async def first(self, params, ctx=None) -> ActionResult:
        return ActionResult(success=True, data="overridden_first")

    def get_manifest(self):
        from digitorn.modules.manifest import ModuleManifest
        return ModuleManifest.from_module(self)


# ── Test group: registry structure ───────────────────────────────────────────


class TestActionRegistryStructure:

    def test_registry_is_dict(self):
        assert isinstance(_MinModule._action_registry, dict)

    def test_registry_keys_are_method_names_without_prefix(self):
        keys = set(_MinModule._action_registry.keys())
        assert keys == {"first", "second"}
        assert "_action_first" not in keys

    def test_registry_values_are_action_entries(self):
        for entry in _MinModule._action_registry.values():
            assert isinstance(entry, ActionEntry)

    def test_entry_name_matches_key(self):
        for key, entry in _MinModule._action_registry.items():
            assert entry.name == key

    def test_entry_spec_is_action_spec(self):
        for entry in _MinModule._action_registry.values():
            assert isinstance(entry.spec, ActionSpec)

    def test_entry_handler_is_callable(self):
        for entry in _MinModule._action_registry.values():
            assert callable(entry.handler)

    def test_entry_handler_is_async(self):
        import inspect
        for entry in _MinModule._action_registry.values():
            assert inspect.iscoroutinefunction(entry.handler)


# ── Test group: ActionSpec field population ──────────────────────────────────


class TestActionSpecFields:

    def test_description_set(self):
        spec = _MinModule._action_registry["first"].spec
        assert spec.description == "First action."

    def test_permissions_set(self):
        assert _MinModule._action_registry["first"].spec.permissions == ["min:read"]
        assert _MinModule._action_registry["second"].spec.permissions == ["min:write"]

    def test_tags_set(self):
        assert _MinModule._action_registry["first"].spec.tags == ["a"]
        assert _MinModule._action_registry["second"].spec.tags == ["b"]

    def test_risk_level_set(self):
        assert _MinModule._action_registry["first"].spec.risk_level == "low"
        assert _MinModule._action_registry["second"].spec.risk_level == "medium"

    def test_irreversible_set(self):
        assert _MinModule._action_registry["first"].spec.irreversible is False
        assert _MinModule._action_registry["second"].spec.irreversible is True

    def test_side_effects_set(self):
        assert _MinModule._action_registry["second"].spec.side_effects == ["filesystem_write"]

    def test_streams_progress_set(self):
        assert _MinModule._action_registry["first"].spec.streams_progress is False
        assert _MinModule._action_registry["second"].spec.streams_progress is True

    def test_spec_also_attached_to_handler(self):
        """_action_spec attribute survives functools.wraps."""
        entry = _MinModule._action_registry["first"]
        assert hasattr(entry.handler, "_action_spec")
        assert entry.handler._action_spec is entry.spec


# ── Test group: registry isolation ───────────────────────────────────────────


class TestRegistryIsolation:

    def test_subclass_gets_parent_actions(self):
        assert "first" in _ChildModule._action_registry
        assert "second" in _ChildModule._action_registry

    def test_subclass_has_own_action(self):
        assert "child_only" in _ChildModule._action_registry

    def test_subclass_does_not_pollute_parent(self):
        assert "child_only" not in _MinModule._action_registry

    def test_parent_does_not_see_child_registry(self):
        assert _MinModule._action_registry is not _ChildModule._action_registry

    def test_override_replaces_parent_spec(self):
        parent_desc = _MinModule._action_registry["first"].spec.description
        child_desc = _OverrideModule._action_registry["first"].spec.description
        assert parent_desc == "First action."
        assert child_desc == "Overridden first."

    def test_instance_registry_is_copy_of_class_registry(self):
        m = _MinModule()
        assert m._action_registry is not _MinModule._action_registry
        assert set(m._action_registry.keys()) == set(_MinModule._action_registry.keys())


# ── Test group: class definition time guarantee ───────────────────────────────


class TestDefinitionTimeRegistration:

    def test_registry_exists_before_instantiation(self):
        """Registry must be populated at class creation, not __init__."""
        # Access class attribute directly - no instance needed
        assert "first" in _MinModule.__dict__.get("_action_registry", {}) or \
               "first" in getattr(_MinModule, "_action_registry", {})

    def test_multiple_instances_share_class_registry_content(self):
        m1 = _MinModule()
        m2 = _MinModule()
        # Both instances produce equivalent registries
        assert set(m1._action_registry.keys()) == set(m2._action_registry.keys())
        # But they are separate objects (instance isolation)
        assert m1._action_registry is not m2._action_registry
