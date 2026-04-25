"""Tests — IModule Protocol, Platform StrEnum, ActionResult, ModuleState/Type, ModuleRegistry.

Covers:
- IModule protocol: isinstance() checks for BaseModule subclasses
- IModule protocol: satisfied by a bare duck-typed class (no inheritance)
- IModule protocol: NOT satisfied by a class missing required members
- Platform StrEnum: equality with plain strings
- Platform StrEnum: membership checks in lists
- Platform.ALL sentinel behaviour
- ActionResult: success/failure path
- ActionResult: generic data field
- ActionResult: to_dict() output
- ActionResult: backward-compat output alias
- ModuleState / ModuleType StrEnum values
- ModuleRegistry: register, get, is_available, list_available
- ModuleRegistry: platform exclusion
- ModuleRegistry: failed module handling
- ModuleRegistry: status_report
"""
from __future__ import annotations

import pytest

from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, Platform
from digitorn.modules.manifest import ModuleManifest
from digitorn.modules.protocol import IModule
from digitorn.modules.registry import ModuleRegistry
from digitorn.modules.types import ModuleState, ModuleType


# ── IModule Protocol ──────────────────────────────────────────────────────────


class TestIModuleProtocol:

    def test_base_module_subclass_satisfies_protocol(self):
        class _M(BaseModule):
            MODULE_ID = "_m1"
            VERSION = "1.0.0"
            def get_manifest(self): return ModuleManifest.from_module(self)

        assert isinstance(_M(), IModule)

    def test_base_module_explicitly_inherits_imodule(self):
        """BaseModule inherits IModule directly — confirmed via MRO."""
        assert IModule in BaseModule.__mro__

    def test_concrete_subclass_in_mro(self):
        """Any concrete module subclass also has IModule in its MRO."""
        class _Concrete(BaseModule):
            MODULE_ID = "concrete"
            VERSION = "1.0.0"
            def get_manifest(self): return ModuleManifest.from_module(self)

        assert IModule in _Concrete.__mro__

    def test_isinstance_works_correctly(self):
        """isinstance() is the correct runtime check — fully supported."""
        class _M(BaseModule):
            MODULE_ID = "_m2"
            VERSION = "1.0.0"
            def get_manifest(self): return ModuleManifest.from_module(self)
        assert isinstance(_M(), IModule)

    def test_duck_typed_class_satisfies_protocol(self):
        """A class with the right shape is recognised as IModule (duck typing)."""

        class DuckModule:
            MODULE_ID = "duck"
            VERSION = "1.0.0"
            SUPPORTED_PLATFORMS = [Platform.ALL]

            async def execute(self, action, params, context=None): pass
            def get_manifest(self): pass
            async def on_start(self): pass
            async def on_stop(self): pass

        assert isinstance(DuckModule(), IModule)

    def test_incomplete_class_does_not_satisfy_protocol(self):
        """Missing execute() means the class is not a valid IModule."""

        class _Broken:
            MODULE_ID = "broken"
            VERSION = "1.0.0"
            SUPPORTED_PLATFORMS = [Platform.ALL]
            # execute() is absent → not an IModule

        assert not isinstance(_Broken(), IModule)

    def test_protocol_requires_get_manifest(self):
        """Missing get_manifest() means the class is not a valid IModule."""

        class _NoManifest:
            MODULE_ID = "nm"
            VERSION = "1.0.0"
            SUPPORTED_PLATFORMS = [Platform.ALL]

            async def execute(self, a, p, c=None): pass
            async def on_start(self): pass
            async def on_stop(self): pass

        assert not isinstance(_NoManifest(), IModule)


# ── Platform StrEnum ──────────────────────────────────────────────────────────


class TestPlatform:

    def test_linux_equals_string(self):
        assert Platform.LINUX == "linux"

    def test_windows_equals_string(self):
        assert Platform.WINDOWS == "windows"

    def test_macos_equals_string(self):
        assert Platform.MACOS == "macos"

    def test_raspberry_pi_equals_string(self):
        assert Platform.RASPBERRY_PI == "raspberry_pi"

    def test_all_equals_string(self):
        assert Platform.ALL == "all"

    def test_membership_with_string(self):
        platforms = [Platform.LINUX, Platform.WINDOWS]
        assert "linux" in platforms

    def test_platform_all_in_list(self):
        platforms = [Platform.ALL]
        assert Platform.ALL in platforms

    def test_str_conversion(self):
        assert str(Platform.LINUX) == "linux"

    def test_is_str_subclass(self):
        assert isinstance(Platform.LINUX, str)


# ── ActionResult ──────────────────────────────────────────────────────────────


class TestActionResult:

    def test_success_result(self):
        r = ActionResult(success=True, data="hello")
        assert r.success is True
        assert r.data == "hello"
        assert r.error is None

    def test_failure_result(self):
        r = ActionResult(success=False, error="something went wrong")
        assert r.success is False
        assert r.error == "something went wrong"
        assert r.data is None

    def test_generic_data_field_any_type(self):
        r: ActionResult[dict] = ActionResult(success=True, data={"key": "val"})
        assert r.data["key"] == "val"

    def test_to_dict_success(self):
        r = ActionResult(success=True, data=42)
        d = r.to_dict()
        assert d["success"] is True
        assert d["data"] == 42
        assert d["error"] is None

    def test_to_dict_failure(self):
        r = ActionResult(success=False, error="bad input")
        d = r.to_dict()
        assert d["success"] is False
        assert d["error"] == "bad input"

    def test_to_dict_uses_data_over_output_alias(self):
        r = ActionResult(success=True, data="primary", output="legacy")
        assert r.to_dict()["data"] == "primary"

    def test_output_alias_fallback_when_no_data(self):
        """When data is None, to_dict() falls back to output (backward compat)."""
        r = ActionResult(success=True, output="legacy_value")
        d = r.to_dict()
        assert d["data"] == "legacy_value"

    def test_metadata_field_default_empty(self):
        r = ActionResult(success=True)
        assert r.metadata == {}

    def test_metadata_can_be_set(self):
        r = ActionResult(success=True, metadata={"cached": True})
        assert r.metadata["cached"] is True


# ── ModuleState and ModuleType ─────────────────────────────────────────────────


class TestModuleState:

    def test_state_values_are_strings(self):
        for state in ModuleState:
            assert isinstance(state, str)

    def test_active_equals_string(self):
        assert ModuleState.ACTIVE == "active"

    def test_error_equals_string(self):
        assert ModuleState.ERROR == "error"

    def test_all_states_present(self):
        expected = {"loaded", "starting", "active", "paused", "stopping", "disabled", "error"}
        actual = {s.value for s in ModuleState}
        assert actual == expected


class TestModuleType:

    def test_system_equals_string(self):
        assert ModuleType.SYSTEM == "system"

    def test_user_equals_string(self):
        assert ModuleType.USER == "user"

    def test_is_str_subclass(self):
        assert isinstance(ModuleType.USER, str)


# ── ModuleRegistry ────────────────────────────────────────────────────────────


class _RegModule(BaseModule):
    MODULE_ID = "reg_mod"
    VERSION   = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]

    def get_manifest(self): return ModuleManifest.from_module(self)


class _PiModule(BaseModule):
    MODULE_ID = "pi_mod"
    VERSION   = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.RASPBERRY_PI]

    def get_manifest(self): return ModuleManifest.from_module(self)


class _BrokenModule(BaseModule):
    MODULE_ID = "broken_mod"
    VERSION   = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]

    def _check_dependencies(self):
        raise ImportError("required_lib is not installed")

    def get_manifest(self): return ModuleManifest.from_module(self)


class TestModuleRegistry:

    def test_register_and_get(self):
        reg = ModuleRegistry()
        reg.register(_RegModule)
        m = reg.get("reg_mod")
        assert isinstance(m, _RegModule)

    def test_get_returns_same_instance(self):
        reg = ModuleRegistry()
        reg.register(_RegModule)
        assert reg.get("reg_mod") is reg.get("reg_mod")

    def test_is_available_true(self):
        reg = ModuleRegistry()
        reg.register(_RegModule)
        assert reg.is_available("reg_mod") is True

    def test_is_available_false_for_unknown(self):
        reg = ModuleRegistry()
        assert reg.is_available("does_not_exist") is False

    def test_list_available(self):
        reg = ModuleRegistry()
        reg.register(_RegModule)
        assert "reg_mod" in reg.list_available()

    def test_unregister(self):
        reg = ModuleRegistry()
        reg.register(_RegModule)
        reg.unregister("reg_mod")
        assert not reg.is_available("reg_mod")

    def test_register_instance_directly(self):
        reg = ModuleRegistry()
        instance = _RegModule()
        reg.register_instance(instance)
        assert reg.get("reg_mod") is instance

    def test_broken_module_goes_to_failed(self):
        from digitorn.modules.exceptions import ModuleLoadError
        reg = ModuleRegistry()
        reg.register(_BrokenModule)
        with pytest.raises(ModuleLoadError):
            reg.get("broken_mod")
        assert "broken_mod" in reg.list_failed()

    def test_platform_excluded_module_reported(self):
        from digitorn.modules.platform_guard import PlatformGuard

        class _FakePlatformInfo:
            os_type = Platform.LINUX

        class _FakeGuard:
            platform_info = _FakePlatformInfo()
            def is_compatible(self, module_class):
                return Platform.RASPBERRY_PI not in module_class.SUPPORTED_PLATFORMS

        reg = ModuleRegistry(platform_guard=_FakeGuard())
        reg.register(_PiModule)
        assert "pi_mod" in reg.list_platform_excluded()
        assert "pi_mod" not in reg.list_available()

    def test_status_report_structure(self):
        reg = ModuleRegistry()
        reg.register(_RegModule)
        report = reg.status_report()
        assert "available" in report
        assert "failed" in report
        assert "platform_excluded" in report

    def test_all_manifests_returns_list(self):
        reg = ModuleRegistry()
        reg.register(_RegModule)
        manifests = reg.all_manifests()
        assert isinstance(manifests, list)
        assert len(manifests) == 1
        assert manifests[0].module_id == "reg_mod"

    def test_get_manifest_delegates(self):
        reg = ModuleRegistry()
        reg.register(_RegModule)
        manifest = reg.get_manifest("reg_mod")
        assert manifest.module_id == "reg_mod"

    def test_module_without_module_id_raises(self):
        class _NoId(BaseModule):
            MODULE_ID = ""
            VERSION   = "1.0.0"
            def get_manifest(self): return ModuleManifest.from_module(self)

        reg = ModuleRegistry()
        with pytest.raises(ValueError, match="MODULE_ID"):
            reg.register(_NoId)
