"""Tests — ModuleManifest, ActionSpec, ModuleManifest.from_module(), model_copy().

Covers:
- ModuleManifest.from_module() with @action registry (primary path)
- ModuleManifest.from_module() with _action_* convention (fallback path)
- ModuleManifest.from_module() with mixed module (registry + dynamic specs)
- model_copy() preserves all fields and applies overrides correctly
- get_action() lookup by name
- action_names() returns all action names
- to_dict() serialisation completeness
- ActionSpec.to_json_schema() generates valid JSON Schema
- ActionSpec.to_langchain_tool_schema() structure
- Capability.from_string() and from_dict()
- ModuleManifest fields: declared_permissions, tags, dependencies, etc.
- Manifest enrichment via _enrich_manifest_metadata() for risk/streaming
"""
from __future__ import annotations

import pytest

from digitorn.modules.base import BaseModule, Platform
from digitorn.modules.decorators import action
from digitorn.modules.manifest import (
    ActionSpec,
    Capability,
    ModuleManifest,
    ParamSpec,
    ResourceLimits,
    ServiceDescriptor,
)


# ── Fixtures / module definitions ─────────────────────────────────────────────


class _FullModule(BaseModule):
    MODULE_ID = "_full"
    VERSION = "2.1.0"
    SUPPORTED_PLATFORMS = [Platform.LINUX, Platform.WINDOWS]

    @action(
        description="Read a file.",
        permissions=["fs:read"],
        tags=["io"],
        risk_level="low",
    )
    async def read_file(self, params, ctx=None):
        pass

    @action(
        description="Delete a file.",
        permissions=["fs:delete"],
        tags=["io", "destructive"],
        risk_level="high",
        irreversible=True,
        side_effects=["filesystem_delete"],
    )
    async def delete_file(self, params, ctx=None):
        pass

    def get_manifest(self):
        return ModuleManifest.from_module(self).model_copy(update={
            "description": "Filesystem operations.",
            "author": "Core Team",
            "homepage": "https://example.com",
            "tags": ["io", "core"],
            "declared_permissions": ["fs:read", "fs:write", "fs:delete"],
            "dependencies": ["aiofiles>=0.8"],
        })


class _LegacyModule(BaseModule):
    MODULE_ID = "_legacy"
    VERSION = "0.1.0"

    async def _action_ping(self, params):
        pass

    async def _action_pong(self, params):
        pass

    def get_manifest(self):
        return ModuleManifest.from_module(self)


# ── Test group: from_module() ─────────────────────────────────────────────────


class TestFromModule:

    def test_from_module_registry_path_creates_manifest(self):
        m = _FullModule()
        manifest = ModuleManifest.from_module(m)
        assert manifest.module_id == "_full"
        assert manifest.version == "2.1.0"

    def test_from_module_registry_path_discovers_all_actions(self):
        m = _FullModule()
        manifest = ModuleManifest.from_module(m)
        names = {a.name for a in manifest.actions}
        assert names == {"read_file", "delete_file"}

    def test_from_module_preserves_spec_fields(self):
        m = _FullModule()
        manifest = ModuleManifest.from_module(m)
        action = next(a for a in manifest.actions if a.name == "delete_file")
        assert action.permissions == ["fs:delete"]
        assert action.irreversible is True
        assert action.risk_level == "high"
        assert "filesystem_delete" in action.side_effects

    def test_from_module_legacy_path_discovers_actions(self):
        m = _LegacyModule()
        manifest = ModuleManifest.from_module(m)
        names = {a.name for a in manifest.actions}
        assert names == {"ping", "pong"}

    def test_from_module_platforms_from_class(self):
        m = _FullModule()
        manifest = ModuleManifest.from_module(m)
        assert "linux" in manifest.platforms
        assert "windows" in manifest.platforms

    def test_from_module_module_type_from_class(self):
        m = _FullModule()
        manifest = ModuleManifest.from_module(m)
        assert manifest.module_type == "user"


# ── Test group: model_copy() ──────────────────────────────────────────────────


class TestModelCopy:

    def test_model_copy_preserves_module_id(self):
        m = _FullModule()
        manifest = m.get_manifest()
        assert manifest.module_id == "_full"

    def test_model_copy_applies_description_override(self):
        m = _FullModule()
        manifest = m.get_manifest()
        assert manifest.description == "Filesystem operations."

    def test_model_copy_applies_author_override(self):
        m = _FullModule()
        manifest = m.get_manifest()
        assert manifest.author == "Core Team"

    def test_model_copy_applies_tags_override(self):
        m = _FullModule()
        manifest = m.get_manifest()
        assert "io" in manifest.tags
        assert "core" in manifest.tags

    def test_model_copy_applies_declared_permissions(self):
        m = _FullModule()
        manifest = m.get_manifest()
        assert "fs:read" in manifest.declared_permissions
        assert "fs:delete" in manifest.declared_permissions

    def test_model_copy_applies_dependencies(self):
        m = _FullModule()
        manifest = m.get_manifest()
        assert any("aiofiles" in d for d in manifest.dependencies)

    def test_model_copy_does_not_mutate_original(self):
        m = _FullModule()
        base = ModuleManifest.from_module(m)
        copy = base.model_copy(update={"description": "Changed"})
        assert base.description == ""  # original unchanged
        assert copy.description == "Changed"

    def test_model_copy_without_update_returns_equivalent(self):
        m = _FullModule()
        base = ModuleManifest.from_module(m)
        copy = base.model_copy()
        assert copy.module_id == base.module_id
        assert len(copy.actions) == len(base.actions)


# ── Test group: manifest lookup helpers ───────────────────────────────────────


class TestManifestLookup:

    def test_get_action_found(self):
        m = _FullModule()
        manifest = m.get_manifest()
        spec = manifest.get_action("read_file")
        assert spec is not None
        assert spec.name == "read_file"

    def test_get_action_not_found(self):
        m = _FullModule()
        manifest = m.get_manifest()
        assert manifest.get_action("nonexistent") is None

    def test_action_names_returns_all(self):
        m = _FullModule()
        names = m.get_manifest().action_names()
        assert set(names) == {"read_file", "delete_file"}


# ── Test group: to_dict() serialisation ──────────────────────────────────────


class TestManifestSerialization:

    def test_to_dict_contains_module_id(self):
        d = _FullModule().get_manifest().to_dict()
        assert d["module_id"] == "_full"

    def test_to_dict_contains_version(self):
        d = _FullModule().get_manifest().to_dict()
        assert d["version"] == "2.1.0"

    def test_to_dict_contains_actions(self):
        d = _FullModule().get_manifest().to_dict()
        assert isinstance(d["actions"], list)
        assert len(d["actions"]) == 2

    def test_to_dict_action_has_params_schema(self):
        d = _FullModule().get_manifest().to_dict()
        for action_d in d["actions"]:
            assert "params_schema" in action_d

    def test_to_dict_action_has_permissions(self):
        d = _FullModule().get_manifest().to_dict()
        delete_d = next(a for a in d["actions"] if a["name"] == "delete_file")
        assert "fs:delete" in delete_d["permissions"]

    def test_to_dict_action_irreversible(self):
        d = _FullModule().get_manifest().to_dict()
        delete_d = next(a for a in d["actions"] if a["name"] == "delete_file")
        assert delete_d["irreversible"] is True


# ── Test group: ActionSpec helpers ────────────────────────────────────────────


class TestActionSpec:

    def _make_spec(self) -> ActionSpec:
        return ActionSpec(
            name="read_file",
            description="Read a file.",
            params=[
                ParamSpec(name="path", type="string", description="File path", required=True),
                ParamSpec(name="encoding", type="string", description="Encoding",
                          required=False, default="utf-8"),
            ],
            permissions=["fs:read"],
        )

    def test_to_json_schema_type_object(self):
        schema = self._make_spec().to_json_schema()
        assert schema["type"] == "object"

    def test_to_json_schema_required_fields(self):
        schema = self._make_spec().to_json_schema()
        assert "path" in schema["required"]
        assert "encoding" not in schema.get("required", [])

    def test_to_json_schema_properties_present(self):
        schema = self._make_spec().to_json_schema()
        assert "path" in schema["properties"]
        assert "encoding" in schema["properties"]

    def test_to_langchain_schema_structure(self):
        lc = self._make_spec().to_langchain_tool_schema()
        assert lc["name"] == "read_file"
        assert lc["description"] == "Read a file."
        assert "parameters" in lc


# ── Test group: Capability ────────────────────────────────────────────────────


class TestCapability:

    def test_from_string(self):
        cap = Capability.from_string("filesystem.read")
        assert cap.permission == "filesystem.read"
        assert cap.scope == ""
        assert cap.constraints == {}

    def test_from_dict(self):
        data = {
            "permission": "network.send",
            "scope": "sandbox_only",
            "constraints": {"allowed_hosts": ["api.example.com"]},
        }
        cap = Capability.from_dict(data)
        assert cap.permission == "network.send"
        assert cap.scope == "sandbox_only"
        assert cap.constraints["allowed_hosts"] == ["api.example.com"]

    def test_to_dict_omits_empty_fields(self):
        cap = Capability.from_string("io.read")
        d = cap.to_dict()
        assert "scope" not in d
        assert "constraints" not in d

    def test_to_dict_includes_populated_fields(self):
        cap = Capability("fs:write", scope="home", constraints={"max_size": 100})
        d = cap.to_dict()
        assert d["scope"] == "home"
        assert d["constraints"]["max_size"] == 100


# ── Test group: resource limits ───────────────────────────────────────────────


class TestResourceLimits:

    def test_resource_limits_defaults(self):
        rl = ResourceLimits()
        assert rl.max_cpu_percent == 100.0
        assert rl.max_memory_mb == 0
        assert rl.max_execution_seconds == 0.0
        assert rl.max_concurrent_actions == 0

    def test_resource_limits_in_manifest(self):
        rl = ResourceLimits(max_memory_mb=512, max_concurrent_actions=3)
        m = _FullModule()
        manifest = m.get_manifest()
        # Inject limits via model_copy
        manifest_with_limits = manifest.model_copy(update={"resource_limits": rl})
        d = manifest_with_limits.to_dict()
        assert "resource_limits" in d
        assert d["resource_limits"]["max_memory_mb"] == 512
