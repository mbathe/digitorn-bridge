"""Shared fixtures and mocks for the module-layer test suite.

Provides lightweight stubs for every external dependency the module layer
imports so that tests run without the full daemon stack installed.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Stub heavy optional dependencies before any module import
# ---------------------------------------------------------------------------

def _make_stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.collect_security_metadata = lambda h: {}          # type: ignore[attr-defined]
    mod.collect_streaming_metadata = lambda h: {}         # type: ignore[attr-defined]
    return mod


_STUBS = [
    "digitorn.security",
    "digitorn.security.decorators",
    "digitorn.cache",
    "digitorn.orchestration",
    "digitorn.orchestration.streaming_decorators",
    "digitorn.cache.client",
    "digitorn.cache.decorators",
]

for _stub_name in _STUBS:
    if _stub_name not in sys.modules:
        sys.modules[_stub_name] = _make_stub(_stub_name)

# Ensure digitorn package root is importable
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[2] / "packages"))


# ---------------------------------------------------------------------------
# Re-usable fixtures
# ---------------------------------------------------------------------------

from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, Platform
from digitorn.modules.decorators import ActionEntry, action
from digitorn.modules.exceptions import ActionExecutionError, ActionNotFoundError
from digitorn.modules.manifest import ActionSpec, ModuleManifest
from digitorn.modules.protocol import IModule
from digitorn.modules.types import ModuleState, ModuleType


@pytest.fixture()
def ctx() -> ExecutionContext:
    """A minimal, fully-frozen ExecutionContext for unit tests."""
    return ExecutionContext(plan_id="test-plan", action_id="test-action")


@pytest.fixture()
def ctx_with_bus() -> ExecutionContext:
    """ExecutionContext with a mock ServiceBus."""
    bus = MagicMock()
    bus.call = AsyncMock(return_value={"mock": True})
    return ExecutionContext(
        plan_id="test-plan",
        action_id="test-action",
        service_bus=bus,
    )


# ---------------------------------------------------------------------------
# Concrete test modules used across multiple test files
# ---------------------------------------------------------------------------

class CalculatorModule(BaseModule):
    """Simple sync-like module using @action decorator."""

    MODULE_ID = "calculator"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]

    @action(
        description="Add two numbers.",
        permissions=["calculator:read"],
        tags=["math"],
    )
    async def add(self, params: dict, ctx: ExecutionContext | None = None) -> ActionResult:
        a = params.get("a", 0)
        b = params.get("b", 0)
        return ActionResult(success=True, data=a + b)

    @action(
        description="Divide a by b.",
        permissions=["calculator:read"],
        risk_level="low",
        tags=["math"],
    )
    async def divide(self, params: dict, ctx: ExecutionContext | None = None) -> ActionResult:
        a = params.get("a", 0)
        b = params.get("b", 1)
        if b == 0:
            return ActionResult(success=False, error="Division by zero")
        return ActionResult(success=True, data=a / b)

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": "Basic arithmetic operations.",
            "author": "Test Suite",
            "tags": ["math", "test"],
        })


class EchoModule(BaseModule):
    """Module using legacy _action_ naming convention."""

    MODULE_ID = "echo"
    VERSION   = "0.5.0"
    SUPPORTED_PLATFORMS = [Platform.LINUX, Platform.WINDOWS, Platform.MACOS]

    async def _action_echo(self, params: dict) -> ActionResult:
        return ActionResult(success=True, data=params.get("text", ""))

    async def _action_fail(self, params: dict) -> ActionResult:
        raise ValueError("deliberate failure")

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self)


class SubclassModule(CalculatorModule):
    """Subclass that adds one more action - tests registry inheritance."""

    MODULE_ID = "calc_plus"

    @action(
        description="Multiply two numbers.",
        permissions=["calculator:read"],
        tags=["math"],
    )
    async def multiply(self, params: dict, ctx: ExecutionContext | None = None) -> ActionResult:
        return ActionResult(success=True, data=params.get("a", 0) * params.get("b", 0))

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self)


@pytest.fixture()
def calc() -> CalculatorModule:
    return CalculatorModule()


@pytest.fixture()
def echo() -> EchoModule:
    return EchoModule()


@pytest.fixture()
def calc_plus() -> SubclassModule:
    return SubclassModule()
