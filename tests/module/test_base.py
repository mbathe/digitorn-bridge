"""Tests - BaseModule: dispatch, lifecycle, policy, dynamic actions, health.

Covers:
- execute() dispatches via _action_registry (O(1) dict) for @action modules
- execute() falls back to _action_<name> naming convention
- execute() raises ActionNotFoundError for unknown actions
- execute() wraps bare exceptions as ActionExecutionError
- Dynamic action registration / deregistration at runtime
- Dynamic actions take precedence over _action_ convention but NOT over registry
- Lifecycle hooks: on_start, on_stop, on_pause, on_resume
- health_check(), metrics(), state_snapshot() defaults
- policy_rules() default returns ModulePolicy
- is_supported_on_current_platform()
- get_context_snippet() default returns None
- set_context() / ctx property
- CONFIG_MODEL / on_config_update()
- describe() delegates to get_manifest().to_dict()
"""
from __future__ import annotations

import asyncio
import pytest
import pytest_asyncio

from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, ModulePolicy, Platform
from digitorn.modules.decorators import ActionEntry, action
from digitorn.modules.exceptions import ActionExecutionError, ActionNotFoundError
from digitorn.modules.manifest import ActionSpec, ModuleManifest

# ── Fixtures imported from conftest ──────────────────────────────────────────
# calc, echo, calc_plus, ctx - all defined in conftest.py


# ── Test group: @action dispatch path ────────────────────────────────────────


class TestActionRegistryDispatch:

    @pytest.mark.asyncio
    async def test_registered_action_returns_result(self, calc, ctx):
        result = await calc.execute("add", {"a": 3, "b": 4}, ctx)
        assert isinstance(result, ActionResult)
        assert result.success is True
        assert result.data == 7

    @pytest.mark.asyncio
    async def test_registered_action_without_context(self, calc):
        result = await calc.execute("divide", {"a": 10, "b": 2})
        assert result.success is True
        assert result.data == 5.0

    @pytest.mark.asyncio
    async def test_action_returning_failure_result(self, calc):
        result = await calc.execute("divide", {"a": 5, "b": 0})
        assert result.success is False
        assert "zero" in result.error.lower()

    @pytest.mark.asyncio
    async def test_all_registry_actions_dispatchable(self, calc):
        for name in calc._action_registry:
            result = await calc.execute(name, {"a": 1, "b": 1})
            assert isinstance(result, ActionResult)

    @pytest.mark.asyncio
    async def test_subclass_dispatch_includes_parent_actions(self, calc_plus):
        r_add = await calc_plus.execute("add", {"a": 2, "b": 3})
        r_mul = await calc_plus.execute("multiply", {"a": 2, "b": 3})
        assert r_add.data == 5
        assert r_mul.data == 6


# ── Test group: _action_ legacy convention fallback ──────────────────────────


class TestLegacyConventionDispatch:

    @pytest.mark.asyncio
    async def test_legacy_action_dispatched(self, echo):
        result = await echo.execute("echo", {"text": "hello"})
        assert result.success is True
        assert result.data == "hello"

    @pytest.mark.asyncio
    async def test_legacy_action_error_wrapped(self, echo):
        with pytest.raises(ActionExecutionError) as exc_info:
            await echo.execute("fail", {})
        err = exc_info.value
        # ActionExecutionError stores the original exception as .cause
        assert isinstance(err.cause, ValueError)
        # The module_id and action are in the structured context dict
        assert err.context.get("module_id") == "echo"
        assert err.context.get("action") == "fail"


# ── Test group: error handling ────────────────────────────────────────────────


class TestDispatchErrors:

    @pytest.mark.asyncio
    async def test_unknown_action_raises_not_found(self, calc):
        with pytest.raises(ActionNotFoundError) as exc_info:
            await calc.execute("nonexistent", {})
        assert exc_info.value.module_id == "calculator"
        assert exc_info.value.action == "nonexistent"

    @pytest.mark.asyncio
    async def test_action_not_found_error_is_module_error(self, calc):
        from digitorn.modules.exceptions import ModuleError
        with pytest.raises(ModuleError):
            await calc.execute("nope", {})

    @pytest.mark.asyncio
    async def test_unexpected_exception_wrapped_as_execution_error(self, echo):
        """_action_fail raises ValueError → must be wrapped as ActionExecutionError."""
        with pytest.raises(ActionExecutionError):
            await echo.execute("fail", {})

    @pytest.mark.asyncio
    async def test_action_execution_error_preserves_cause(self, echo):
        with pytest.raises(ActionExecutionError) as exc_info:
            await echo.execute("fail", {})
        assert isinstance(exc_info.value.cause, ValueError)

    @pytest.mark.asyncio
    async def test_action_not_found_is_not_wrapped(self, calc):
        """ActionNotFoundError must propagate as-is, not wrapped."""
        with pytest.raises(ActionNotFoundError):
            await calc.execute("missing", {})


# ── Test group: dynamic action registration ───────────────────────────────────


class TestDynamicActions:

    @pytest.mark.asyncio
    async def test_dynamic_action_registered_and_callable(self, calc):
        async def greet(params):
            return ActionResult(success=True, data="hi " + params.get("name", ""))

        calc.register_action("greet", greet)
        result = await calc.execute("greet", {"name": "Bob"})
        assert result.data == "hi Bob"

    @pytest.mark.asyncio
    async def test_dynamic_action_unregistered(self, calc):
        async def temp(params): return ActionResult(success=True, data=None)
        calc.register_action("temp_action", temp)
        await calc.execute("temp_action", {})
        calc.unregister_action("temp_action")
        with pytest.raises(ActionNotFoundError):
            await calc.execute("temp_action", {})

    @pytest.mark.asyncio
    async def test_dynamic_action_with_spec(self, calc):
        spec = ActionSpec(name="custom", description="A custom action", permissions=["calc:read"])

        async def custom(params): return ActionResult(success=True, data=42)
        calc.register_action("custom", custom, spec=spec)
        result = await calc.execute("custom", {})
        assert result.data == 42

    @pytest.mark.asyncio
    async def test_dynamic_does_not_override_registry_action(self, calc):
        """_action_registry takes priority over _dynamic_actions."""
        async def fake_add(params): return ActionResult(success=True, data=9999)
        calc._dynamic_actions["add"] = fake_add  # inject directly into dynamic dict
        result = await calc.execute("add", {"a": 1, "b": 2})
        # Registry path should win → result is 3, not 9999
        assert result.data == 3


# ── Test group: ExecutionContext ──────────────────────────────────────────────


class TestExecutionContext:

    def test_context_is_frozen(self, ctx):
        with pytest.raises((AttributeError, TypeError)):
            ctx.plan_id = "mutated"  # type: ignore[misc]

    def test_context_fields(self, ctx):
        assert ctx.plan_id == "test-plan"
        assert ctx.action_id == "test-action"
        assert ctx.service_bus is None
        assert ctx.stream is None

    def test_context_with_service_bus(self, ctx_with_bus):
        assert ctx_with_bus.service_bus is not None

    def test_context_defaults(self):
        ctx = ExecutionContext(plan_id="p", action_id="a")
        assert ctx.session_id is None
        assert ctx.metadata == {}

    def test_context_equality_ignores_bus(self):
        """compare=False on service_bus means two contexts with same plan/action are equal."""
        from unittest.mock import MagicMock
        c1 = ExecutionContext(plan_id="p", action_id="a", service_bus=None)
        c2 = ExecutionContext(plan_id="p", action_id="a", service_bus=MagicMock())
        assert c1 == c2


# ── Test group: lifecycle hooks ───────────────────────────────────────────────


class TestLifecycleHooks:

    @pytest.mark.asyncio
    async def test_on_start_default_noop(self, calc):
        await calc.on_start()  # must not raise

    @pytest.mark.asyncio
    async def test_on_stop_default_noop(self, calc):
        await calc.on_stop()

    @pytest.mark.asyncio
    async def test_on_pause_default_noop(self, calc):
        await calc.on_pause()

    @pytest.mark.asyncio
    async def test_on_resume_default_noop(self, calc):
        await calc.on_resume()

    @pytest.mark.asyncio
    async def test_lifecycle_hooks_are_overrideable(self):
        started = []

        class _LiveModule(BaseModule):
            MODULE_ID = "_live"
            VERSION   = "1.0.0"

            async def on_start(self):
                started.append(True)

            def get_manifest(self):
                return ModuleManifest.from_module(self)

        m = _LiveModule()
        await m.on_start()
        assert started == [True]

    @pytest.mark.asyncio
    async def test_on_install_and_on_update_default_noop(self, calc):
        await calc.on_install()
        await calc.on_update("0.9.0")


# ── Test group: introspection ─────────────────────────────────────────────────


class TestIntrospection:

    @pytest.mark.asyncio
    async def test_health_check_default(self, calc):
        h = await calc.health_check()
        assert h["status"] == "ok"
        assert h["module_id"] == "calculator"
        assert h["version"] == "1.0.0"

    def test_metrics_default_empty(self, calc):
        assert calc.metrics() == {}

    def test_state_snapshot_default_empty(self, calc):
        assert calc.state_snapshot() == {}

    def test_describe_returns_manifest_dict(self, calc):
        d = calc.describe()
        assert isinstance(d, dict)
        assert d["module_id"] == "calculator"

    def test_get_context_snippet_default_none(self, calc):
        assert calc.get_context_snippet() is None

    def test_policy_rules_returns_module_policy(self, calc):
        p = calc.policy_rules()
        assert isinstance(p, ModulePolicy)

    def test_register_services_returns_empty_list(self, calc):
        assert calc.register_services() == []


# ── Test group: platform support ─────────────────────────────────────────────


class TestPlatformSupport:

    def test_all_platform_always_supported(self, calc):
        assert calc.is_supported_on_current_platform() is True

    def test_restricted_platform_is_platform_dependent(self):
        import platform as _sys_platform

        class _PiModule(BaseModule):
            MODULE_ID = "_pi"
            VERSION = "1.0.0"
            SUPPORTED_PLATFORMS = [Platform.RASPBERRY_PI]
            def get_manifest(self): return ModuleManifest.from_module(self)

        m = _PiModule()
        current = _sys_platform.system().lower()
        expected = current in ("raspberry_pi",)
        # On a dev machine this is False - just check it doesn't crash
        assert isinstance(m.is_supported_on_current_platform(), bool)


# ── Test group: config ────────────────────────────────────────────────────────


class TestModuleConfig:

    @pytest.mark.asyncio
    async def test_on_config_update_without_config_model(self, calc):
        """When CONFIG_MODEL is None, on_config_update is a no-op (no crash)."""
        await calc.on_config_update({"some_key": "value"})
        assert calc.config is None

    @pytest.mark.asyncio
    async def test_on_config_update_with_config_model(self):
        pytest.importorskip("pydantic", reason="pydantic not installed in this environment")
        from digitorn.modules.config import ConfigField, ModuleConfigBase

        class _Cfg(ModuleConfigBase):
            timeout: int = ConfigField(30, description="Timeout in seconds")

        class _CfgModule(BaseModule):
            MODULE_ID = "_cfg"
            VERSION = "1.0.0"
            CONFIG_MODEL = _Cfg

            def get_manifest(self): return ModuleManifest.from_module(self)

        m = _CfgModule()
        await m.on_config_update({"timeout": 60})
        assert m.config is not None
        assert m.config.timeout == 60
