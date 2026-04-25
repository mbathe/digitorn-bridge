"""Tests for watcher primitives on the context_builder module.

Covers:
- Watcher dataclass (to_summary, add_history, history ring buffer)
- All 7 watcher actions (watch_start/stop/pause/resume/status/list/history)
- Watcher loop (_watcher_loop) with real asyncio
- All 5 escalation strategies (on_change, on_error, on_threshold, summary, always)
- Threshold evaluator (_eval_threshold, _resolve_dot_path, _parse_literal)
- Notification queue (push, drain, format)
- has_active_bg_tasks integration with watchers
- on_stop cleanup
- Edge cases (unknown watcher, invalid strategy, approval-required tools)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from digitorn.modules.context_builder.builder import build_index
from digitorn.modules.context_builder.module import (
    ContextBuilderModule,
    Watcher,
)
from digitorn.modules.context_builder.params import (
    WatcherIdParams,
    WatchHistoryParams,
    WatchListParams,
    WatchStartParams,
)


# ---------------------------------------------------------------------------
# Helpers — fake modules with realistic action registries
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


def _make_module(
    module_id: str,
    actions: dict[str, dict[str, Any]],
    description: str = "",
) -> MagicMock:
    """Create a mock module with a realistic _action_registry."""
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

    manifest = MagicMock()
    manifest.description = description or f"Module {module_id}"
    module.get_manifest.return_value = manifest

    module.execute = AsyncMock()
    return module


def _make_security_profile(
    *,
    default_policy: str = "auto",
    max_risk_level: str = "high",
    blocked_actions: dict[str, list[str]] | None = None,
    approve_actions: dict[str, list[str]] | None = None,
):
    """Create a fake SecurityProfile for testing."""
    from digitorn.core.security import ModuleGrant, SecurityProfile

    grants: dict[str, ModuleGrant] = {}
    for mid, actions in (blocked_actions or {}).items():
        overrides = {a: "block" for a in actions}
        grants[mid] = ModuleGrant(module_id=mid, action_overrides=overrides)
    for mid, actions in (approve_actions or {}).items():
        overrides = {a: "approve" for a in actions}
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

    return SecurityProfile(
        app_id="test-app",
        default_policy=default_policy,
        max_risk_level=max_risk_level,
        module_grants=grants,
    )


def _build_cb_with_module(
    module_id: str = "http",
    actions: dict[str, dict[str, Any]] | None = None,
    security_profile: Any | None = None,
    execute_return: Any = None,
) -> tuple[ContextBuilderModule, MagicMock]:
    """Build a ContextBuilderModule with a single fake module indexed."""
    if actions is None:
        actions = {
            "get": {
                "description": "HTTP GET request",
                "risk_level": "low",
                "tags": ["http"],
            },
            "post": {
                "description": "HTTP POST request",
                "risk_level": "medium",
                "tags": ["http"],
            },
        }

    mock_mod = _make_module(module_id, actions, description=f"{module_id} module")
    if execute_return is not None:
        mock_mod.execute.return_value = execute_return

    cb = ContextBuilderModule()
    cb.build_and_set_index({module_id: mock_mod}, security_profile)
    return cb, mock_mod


# ===========================================================================
# Tests — Watcher Dataclass
# ===========================================================================


class TestWatcherDataclass:

    def test_to_summary_fields(self):
        w = Watcher(
            watcher_id="abc123",
            tool_name="http.get",
            params={"url": "https://example.com"},
            interval=30.0,
            label="Test watcher",
            notify_when="on_change",
            notify_config={},
        )
        s = w.to_summary()
        assert s["watcher_id"] == "abc123"
        assert s["tool_name"] == "http.get"
        assert s["label"] == "Test watcher"
        assert s["status"] == "running"
        assert s["interval"] == 30.0
        assert s["notify_when"] == "on_change"
        assert s["check_count"] == 0
        assert s["notify_count"] == 0

    def test_add_history_and_ring_buffer(self):
        w = Watcher(
            watcher_id="x",
            tool_name="http.get",
            params={},
            interval=10,
            label="test",
            notify_when="always",
            notify_config={},
        )
        # Add 105 entries — should keep only last 100
        for i in range(105):
            w.add_history({"check": i})

        assert len(w.history) == 100
        assert w.history[0]["check"] == 5  # oldest kept
        assert w.history[-1]["check"] == 104  # newest

    def test_initial_state(self):
        w = Watcher(
            watcher_id="test",
            tool_name="http.get",
            params={},
            interval=10,
            label="t",
            notify_when="on_change",
            notify_config={},
        )
        assert w.status == "running"
        assert w.check_count == 0
        assert w.notify_count == 0
        assert w.last_result is None
        assert w.last_error is None
        assert w.history == []
        assert w._accumulator == []
        assert w._prev_result is None


# ===========================================================================
# Tests — watch_start
# ===========================================================================


class TestWatchStart:

    @pytest.fixture()
    async def cb(self):
        cb, mod = _build_cb_with_module()
        # Return a simple dict from execute
        from digitorn.modules.base import ActionResult
        mod.execute.return_value = ActionResult(
            success=True, data={"status": 200}
        )
        yield cb
        # Cleanup: stop all watchers
        await cb.on_stop()

    @pytest.mark.asyncio
    async def test_start_creates_watcher(self, cb):
        params = WatchStartParams(
            name="http.get",
            params={"url": "https://example.com"},
            interval=5.0,
            label="Health check",
            notify_when="on_change",
        )
        result = await cb.watch_start(params)

        assert result.success is True
        assert "watcher_id" in result.data
        assert result.data["tool_name"] == "http.get"
        assert result.data["label"] == "Health check"
        assert result.data["status"] == "running"
        assert result.data["interval"] == 5.0

        # Watcher is in the dict
        watcher_id = result.data["watcher_id"]
        assert watcher_id in cb._watchers
        w = cb._watchers[watcher_id]
        assert w._asyncio_task is not None
        assert not w._asyncio_task.done()

    @pytest.mark.asyncio
    async def test_start_default_label(self, cb):
        params = WatchStartParams(name="http.get", params={})
        result = await cb.watch_start(params)
        assert result.success
        assert result.data["label"] == "Watch http.get"

    @pytest.mark.asyncio
    async def test_start_invalid_strategy(self, cb):
        params = WatchStartParams(
            name="http.get",
            params={},
            notify_when="invalid_strategy",
        )
        result = await cb.watch_start(params)
        assert result.success is False
        assert "Invalid notify_when" in result.error

    @pytest.mark.asyncio
    async def test_start_threshold_without_expression(self, cb):
        params = WatchStartParams(
            name="http.get",
            params={},
            notify_when="on_threshold",
            notify_config={},
        )
        result = await cb.watch_start(params)
        assert result.success is False
        assert "expression" in result.error

    @pytest.mark.asyncio
    async def test_start_unknown_tool(self, cb):
        params = WatchStartParams(name="nonexistent.tool", params={})
        result = await cb.watch_start(params)
        assert result.success is False
        assert "not found" in result.error.lower() or "Tool" in result.error

    @pytest.mark.asyncio
    async def test_start_approval_required_tool(self):
        profile = _make_security_profile(
            approve_actions={"http": ["post"]},
        )
        cb, _ = _build_cb_with_module(security_profile=profile)
        params = WatchStartParams(name="http.post", params={})
        result = await cb.watch_start(params)
        assert result.success is False
        assert "approval" in result.error.lower()

    @pytest.mark.asyncio
    async def test_start_blocked_tool(self):
        profile = _make_security_profile(
            blocked_actions={"http": ["post"]},
        )
        cb, _ = _build_cb_with_module(security_profile=profile)
        params = WatchStartParams(name="http.post", params={})
        result = await cb.watch_start(params)
        assert result.success is False
        # Blocked tools are excluded from the index entirely → "not found"
        assert "not found" in result.error.lower() or "blocked" in result.error.lower()

    @pytest.mark.asyncio
    async def test_start_multiple_watchers(self, cb):
        r1 = await cb.watch_start(WatchStartParams(name="http.get", params={"url": "a"}))
        r2 = await cb.watch_start(WatchStartParams(name="http.get", params={"url": "b"}))
        assert r1.success and r2.success
        assert r1.data["watcher_id"] != r2.data["watcher_id"]
        assert len(cb._watchers) == 2

    @pytest.mark.asyncio
    async def test_start_rejects_missing_required_params(self):
        """watch_start should fail immediately if tool params are invalid."""
        from pydantic import BaseModel, Field

        class FakeGetParams(BaseModel):
            url: str = Field(..., description="Target URL.")

        cb, mod = _build_cb_with_module(
            actions={
                "get": {
                    "description": "HTTP GET request",
                    "risk_level": "low",
                    "tags": ["http"],
                    "params_model": FakeGetParams,
                },
            }
        )
        # Call watch_start WITHOUT url in params — should fail immediately
        params = WatchStartParams(name="http.get", params={})
        result = await cb.watch_start(params)
        assert result.success is False
        assert "url" in result.error.lower()
        # No watcher should have been created
        assert len(cb._watchers) == 0
        await cb.on_stop()

    @pytest.mark.asyncio
    async def test_start_accepts_valid_params(self):
        """watch_start should succeed when all required params are present."""
        from pydantic import BaseModel, Field
        from digitorn.modules.base import ActionResult

        class FakeGetParams(BaseModel):
            url: str = Field(..., description="Target URL.")

        cb, mod = _build_cb_with_module(
            actions={
                "get": {
                    "description": "HTTP GET request",
                    "risk_level": "low",
                    "tags": ["http"],
                    "params_model": FakeGetParams,
                },
            }
        )
        mod.execute.return_value = ActionResult(success=True, data={"status": 200})
        params = WatchStartParams(name="http.get", params={"url": "https://example.com"})
        result = await cb.watch_start(params)
        assert result.success is True
        assert len(cb._watchers) == 1
        await cb.on_stop()

    @pytest.mark.asyncio
    async def test_start_with_max_checks(self, cb):
        """max_checks should be stored in the watcher."""
        params = WatchStartParams(
            name="http.get",
            params={"url": "https://example.com"},
            interval=5.0,
            max_checks=3,
            notify_when="always",
        )
        result = await cb.watch_start(params)
        assert result.success
        wid = result.data["watcher_id"]
        assert cb._watchers[wid].max_checks == 3
        assert result.data["max_checks"] == 3

    @pytest.mark.asyncio
    async def test_start_max_checks_zero_means_unlimited(self, cb):
        """max_checks=0 (default) means unlimited."""
        params = WatchStartParams(name="http.get", params={})
        result = await cb.watch_start(params)
        assert result.success
        wid = result.data["watcher_id"]
        assert cb._watchers[wid].max_checks == 0


# ===========================================================================
# Tests — watch_stop
# ===========================================================================


class TestWatchStop:

    @pytest.fixture()
    async def cb_with_watcher(self):
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult
        mod.execute.return_value = ActionResult(success=True, data={"ok": True})
        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
        ))
        watcher_id = result.data["watcher_id"]
        yield cb, watcher_id
        await cb.on_stop()

    @pytest.mark.asyncio
    async def test_stop_removes_watcher(self, cb_with_watcher):
        cb, watcher_id = cb_with_watcher
        result = await cb.watch_stop(WatcherIdParams(watcher_id=watcher_id))
        assert result.success
        assert watcher_id not in cb._watchers
        assert "stopped" in result.data["hint"].lower()

    @pytest.mark.asyncio
    async def test_stop_unknown_id(self):
        cb, _ = _build_cb_with_module()
        result = await cb.watch_stop(WatcherIdParams(watcher_id="nonexistent"))
        assert result.success is False
        assert "not found" in result.error.lower()


# ===========================================================================
# Tests — watch_pause / watch_resume
# ===========================================================================


class TestWatchPauseResume:

    @pytest.fixture()
    async def cb_with_watcher(self):
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult
        mod.execute.return_value = ActionResult(success=True, data={"ok": True})
        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
        ))
        watcher_id = result.data["watcher_id"]
        yield cb, watcher_id
        for w in list(cb._watchers.values()):
            w.status = "stopped"
            if w._asyncio_task and not w._asyncio_task.done():
                w._asyncio_task.cancel()

    @pytest.mark.asyncio
    async def test_pause_running_watcher(self, cb_with_watcher):
        cb, wid = cb_with_watcher
        result = await cb.watch_pause(WatcherIdParams(watcher_id=wid))
        assert result.success
        assert cb._watchers[wid].status == "paused"

    @pytest.mark.asyncio
    async def test_pause_already_paused(self, cb_with_watcher):
        cb, wid = cb_with_watcher
        await cb.watch_pause(WatcherIdParams(watcher_id=wid))
        result = await cb.watch_pause(WatcherIdParams(watcher_id=wid))
        assert result.success is False
        assert "paused" in result.error

    @pytest.mark.asyncio
    async def test_resume_paused_watcher(self, cb_with_watcher):
        cb, wid = cb_with_watcher
        await cb.watch_pause(WatcherIdParams(watcher_id=wid))
        result = await cb.watch_resume(WatcherIdParams(watcher_id=wid))
        assert result.success
        assert cb._watchers[wid].status == "running"

    @pytest.mark.asyncio
    async def test_resume_running_watcher_fails(self, cb_with_watcher):
        cb, wid = cb_with_watcher
        result = await cb.watch_resume(WatcherIdParams(watcher_id=wid))
        assert result.success is False
        assert "running" in result.error

    @pytest.mark.asyncio
    async def test_pause_unknown_watcher(self):
        cb, _ = _build_cb_with_module()
        result = await cb.watch_pause(WatcherIdParams(watcher_id="nope"))
        assert result.success is False

    @pytest.mark.asyncio
    async def test_resume_unknown_watcher(self):
        cb, _ = _build_cb_with_module()
        result = await cb.watch_resume(WatcherIdParams(watcher_id="nope"))
        assert result.success is False


# ===========================================================================
# Tests — watch_status / watch_list / watch_history
# ===========================================================================


class TestWatchStatusListHistory:

    @pytest.fixture()
    async def cb_with_watcher(self):
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult
        mod.execute.return_value = ActionResult(success=True, data={"status": 200})
        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={"url": "test"}, interval=5.0,
            label="API Monitor",
        ))
        watcher_id = result.data["watcher_id"]
        yield cb, watcher_id
        for w in list(cb._watchers.values()):
            w.status = "stopped"
            if w._asyncio_task and not w._asyncio_task.done():
                w._asyncio_task.cancel()

    @pytest.mark.asyncio
    async def test_status_returns_details(self, cb_with_watcher):
        cb, wid = cb_with_watcher
        result = await cb.watch_status(WatcherIdParams(watcher_id=wid))
        assert result.success
        assert result.data["watcher_id"] == wid
        assert result.data["label"] == "API Monitor"
        assert "params" in result.data
        assert "notify_config" in result.data
        assert "recent_history" in result.data

    @pytest.mark.asyncio
    async def test_status_unknown_watcher(self):
        cb, _ = _build_cb_with_module()
        result = await cb.watch_status(WatcherIdParams(watcher_id="nope"))
        assert result.success is False

    @pytest.mark.asyncio
    async def test_list_empty(self):
        cb, _ = _build_cb_with_module()
        result = await cb.watch_list(WatchListParams())
        assert result.success
        assert result.data["total"] == 0
        assert "No watchers" in result.data["hint"]

    @pytest.mark.asyncio
    async def test_list_with_watchers(self, cb_with_watcher):
        cb, wid = cb_with_watcher
        result = await cb.watch_list(WatchListParams())
        assert result.success
        assert result.data["total"] == 1
        assert result.data["running"] == 1
        assert result.data["watchers"][0]["watcher_id"] == wid

    @pytest.mark.asyncio
    async def test_list_with_paused_watcher(self, cb_with_watcher):
        cb, wid = cb_with_watcher
        await cb.watch_pause(WatcherIdParams(watcher_id=wid))
        result = await cb.watch_list(WatchListParams())
        assert result.data["running"] == 0
        assert result.data["paused"] == 1

    @pytest.mark.asyncio
    async def test_history_empty(self, cb_with_watcher):
        cb, wid = cb_with_watcher
        result = await cb.watch_history(WatchHistoryParams(watcher_id=wid))
        assert result.success
        assert result.data["entries"] == []
        assert result.data["total_checks"] == 0

    @pytest.mark.asyncio
    async def test_history_unknown_watcher(self):
        cb, _ = _build_cb_with_module()
        result = await cb.watch_history(WatchHistoryParams(watcher_id="x"))
        assert result.success is False


# ===========================================================================
# Tests — Escalation Strategies (_evaluate_notify)
# ===========================================================================


class TestEscalationStrategies:
    """Test _evaluate_notify with each strategy directly."""

    def _make_watcher(self, strategy: str, **kwargs) -> Watcher:
        return Watcher(
            watcher_id="test",
            tool_name="http.get",
            params={},
            interval=10,
            label="test",
            notify_when=strategy,
            notify_config=kwargs.get("notify_config", {}),
        )

    def test_always_notifies(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("always")
        assert cb._evaluate_notify(w, {"data": 1}, None) is True
        assert cb._evaluate_notify(w, {"data": 1}, None) is True

    # --- on_change ---

    def test_on_change_first_check_establishes_baseline(self):
        """First check silently records baseline — no notification."""
        cb = ContextBuilderModule()
        w = self._make_watcher("on_change")
        w.check_count = 1  # first check
        assert cb._evaluate_notify(w, {"status": 200}, None) is False

    def test_on_change_same_result_no_notify(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("on_change")
        w.check_count = 2
        w._prev_result = {"status": 200}
        w._prev_error = None
        assert cb._evaluate_notify(w, {"status": 200}, None) is False

    def test_on_change_different_result_notifies(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("on_change")
        w.check_count = 2
        w._prev_result = {"status": 200}
        w._prev_error = None
        assert cb._evaluate_notify(w, {"status": 500}, None) is True

    def test_on_change_error_appears_notifies(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("on_change")
        w.check_count = 2
        w._prev_result = {"status": 200}
        w._prev_error = None
        assert cb._evaluate_notify(w, None, "Connection refused") is True

    def test_on_change_error_disappears_notifies(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("on_change")
        w.check_count = 2
        w._prev_result = None
        w._prev_error = "Connection refused"
        assert cb._evaluate_notify(w, {"status": 200}, None) is True

    # --- on_error ---

    def test_on_error_no_error_no_notify(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("on_error")
        w._was_error = False
        assert cb._evaluate_notify(w, {"ok": True}, None) is False

    def test_on_error_error_notifies(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("on_error")
        w._was_error = False
        assert cb._evaluate_notify(w, None, "timeout") is True

    def test_on_error_recovery_notifies(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("on_error")
        w._was_error = True  # was in error state
        assert cb._evaluate_notify(w, {"ok": True}, None) is True

    def test_on_error_consecutive_errors_notify(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("on_error")
        w._was_error = True
        assert cb._evaluate_notify(w, None, "still broken") is True

    # --- on_threshold ---

    def test_on_threshold_condition_met(self):
        cb = ContextBuilderModule()
        w = self._make_watcher(
            "on_threshold",
            notify_config={"expression": "result.status_code != 200"},
        )
        assert cb._evaluate_notify(w, {"status_code": 500}, None) is True

    def test_on_threshold_condition_not_met(self):
        cb = ContextBuilderModule()
        w = self._make_watcher(
            "on_threshold",
            notify_config={"expression": "result.status_code != 200"},
        )
        assert cb._evaluate_notify(w, {"status_code": 200}, None) is False

    def test_on_threshold_no_expression(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("on_threshold", notify_config={})
        assert cb._evaluate_notify(w, {"x": 1}, None) is False

    # --- summary ---

    def test_summary_batches_checks(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("summary", notify_config={"batch_size": 3})

        # First 2 checks: no notification
        assert cb._evaluate_notify(w, {"v": 1}, None) is False
        assert cb._evaluate_notify(w, {"v": 2}, None) is False
        # 3rd check: batch is full → notify
        assert cb._evaluate_notify(w, {"v": 3}, None) is True
        # Accumulator should have 3 items
        assert len(w._accumulator) == 3

    def test_summary_default_batch_size_10(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("summary", notify_config={})
        for i in range(9):
            assert cb._evaluate_notify(w, {"v": i}, None) is False
        assert cb._evaluate_notify(w, {"v": 9}, None) is True

    # --- unknown strategy ---

    def test_unknown_strategy_no_notify(self):
        cb = ContextBuilderModule()
        w = self._make_watcher("nonexistent")
        assert cb._evaluate_notify(w, {"x": 1}, None) is False


# ===========================================================================
# Tests — Threshold Evaluator
# ===========================================================================


class TestThresholdEvaluator:

    def test_equality(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold("result.x == 42", {"x": 42}, None) is True
        assert cb._eval_threshold("result.x == 42", {"x": 99}, None) is False

    def test_not_equal(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold("result.code != 200", {"code": 500}, None) is True
        assert cb._eval_threshold("result.code != 200", {"code": 200}, None) is False

    def test_greater_than(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold("result.count > 100", {"count": 150}, None) is True
        assert cb._eval_threshold("result.count > 100", {"count": 50}, None) is False

    def test_less_than(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold("result.size < 1024", {"size": 512}, None) is True
        assert cb._eval_threshold("result.size < 1024", {"size": 2048}, None) is False

    def test_greater_equal(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold("result.v >= 10", {"v": 10}, None) is True
        assert cb._eval_threshold("result.v >= 10", {"v": 9}, None) is False

    def test_less_equal(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold("result.v <= 5", {"v": 5}, None) is True
        assert cb._eval_threshold("result.v <= 5", {"v": 6}, None) is False

    def test_nested_dot_path(self):
        cb = ContextBuilderModule()
        data = {"response": {"status": {"code": 404}}}
        assert cb._eval_threshold("result.response.status.code == 404", data, None) is True

    def test_null_comparison(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold("result.error != null", {"error": "bad"}, None) is True
        assert cb._eval_threshold("result.error != null", {"error": None}, None) is False

    def test_string_comparison(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold('result.status == "down"', {"status": "down"}, None) is True
        assert cb._eval_threshold('result.status == "down"', {"status": "up"}, None) is False

    def test_boolean_comparison(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold("result.healthy == true", {"healthy": True}, None) is True
        assert cb._eval_threshold("result.healthy == false", {"healthy": False}, None) is True

    def test_float_comparison(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold("result.latency > 1.5", {"latency": 2.0}, None) is True

    def test_error_path(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold("error != null", {}, "something broke") is True
        assert cb._eval_threshold("error != null", {}, None) is False

    def test_missing_field_returns_none(self):
        cb = ContextBuilderModule()
        # result.nonexistent → None, None != 200 → True
        assert cb._eval_threshold("result.nonexistent != 200", {"x": 1}, None) is True

    def test_invalid_expression_no_crash(self):
        cb = ContextBuilderModule()
        assert cb._eval_threshold("no_operator_here", {}, None) is False
        assert cb._eval_threshold("", {}, None) is False

    def test_type_mismatch_no_crash(self):
        cb = ContextBuilderModule()
        # Comparing string to int — should return False, not crash
        assert cb._eval_threshold("result.x > 10", {"x": "hello"}, None) is False


# ===========================================================================
# Tests — _resolve_dot_path
# ===========================================================================


class TestResolveDotPath:

    def test_simple_field(self):
        assert ContextBuilderModule._resolve_dot_path("result.x", {"x": 42}, None) == 42

    def test_nested_field(self):
        data = {"a": {"b": {"c": "deep"}}}
        assert ContextBuilderModule._resolve_dot_path("result.a.b.c", data, None) == "deep"

    def test_error_path(self):
        assert ContextBuilderModule._resolve_dot_path("error", {}, "boom") == "boom"

    def test_missing_field(self):
        assert ContextBuilderModule._resolve_dot_path("result.missing", {"x": 1}, None) is None

    def test_none_intermediate(self):
        assert ContextBuilderModule._resolve_dot_path("result.a.b", {"a": None}, None) is None

    def test_list_index(self):
        data = {"items": [10, 20, 30]}
        assert ContextBuilderModule._resolve_dot_path("result.items.1", data, None) == 20

    def test_no_result_prefix(self):
        # When path doesn't start with "result" or "error", use result as root
        assert ContextBuilderModule._resolve_dot_path("x.y", {"x": {"y": 5}}, None) == 5


# ===========================================================================
# Tests — _parse_literal
# ===========================================================================


class TestParseLiteral:

    def test_null(self):
        assert ContextBuilderModule._parse_literal("null") is None
        assert ContextBuilderModule._parse_literal("None") is None

    def test_booleans(self):
        assert ContextBuilderModule._parse_literal("true") is True
        assert ContextBuilderModule._parse_literal("True") is True
        assert ContextBuilderModule._parse_literal("false") is False
        assert ContextBuilderModule._parse_literal("False") is False

    def test_integers(self):
        assert ContextBuilderModule._parse_literal("42") == 42
        assert ContextBuilderModule._parse_literal("0") == 0
        assert ContextBuilderModule._parse_literal("-5") == -5

    def test_floats(self):
        assert ContextBuilderModule._parse_literal("3.14") == 3.14
        assert ContextBuilderModule._parse_literal("0.5") == 0.5

    def test_quoted_strings(self):
        assert ContextBuilderModule._parse_literal('"hello"') == "hello"
        assert ContextBuilderModule._parse_literal("'world'") == "world"

    def test_unquoted_string(self):
        assert ContextBuilderModule._parse_literal("down") == "down"


# ===========================================================================
# Tests — Watcher Loop Integration (real asyncio)
# ===========================================================================


class TestWatcherLoop:
    """Test the actual watcher loop with real asyncio timing."""

    @pytest.mark.asyncio
    async def test_watcher_executes_checks(self):
        """Start a watcher with 'always' strategy and 5s interval,
        verify it actually executes checks."""
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult

        call_count = 0
        async def _fake_execute(action, params, **kwargs):
            nonlocal call_count
            call_count += 1
            return ActionResult(success=True, data={"count": call_count})

        mod.execute = _fake_execute

        result = await cb.watch_start(WatchStartParams(
            name="http.get",
            params={},
            interval=5.0,  # minimum
            notify_when="always",
            label="loop test",
        ))
        assert result.success
        wid = result.data["watcher_id"]

        # Wait for ~2 checks (interval is 5s, so ~11s)
        await asyncio.sleep(11)

        w = cb._watchers[wid]
        assert w.check_count >= 2
        assert w.notify_count >= 2
        assert w.last_result is not None

        # Verify notifications were queued
        notifications = cb.drain_bg_notifications()
        assert len(notifications) >= 2
        assert all(n["type"] == "watcher" for n in notifications)
        assert all(n["watcher_id"] == wid for n in notifications)

        # Cleanup
        await cb.watch_stop(WatcherIdParams(watcher_id=wid))

    @pytest.mark.asyncio
    async def test_paused_watcher_skips_checks(self):
        """Paused watcher should not increment check_count."""
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult
        mod.execute.return_value = ActionResult(success=True, data={"ok": True})

        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0, notify_when="always",
        ))
        wid = result.data["watcher_id"]

        # Immediately pause
        await cb.watch_pause(WatcherIdParams(watcher_id=wid))
        initial_count = cb._watchers[wid].check_count

        # Wait past an interval
        await asyncio.sleep(7)

        assert cb._watchers[wid].check_count == initial_count

        # Cleanup
        await cb.watch_stop(WatcherIdParams(watcher_id=wid))

    @pytest.mark.asyncio
    async def test_on_change_suppresses_identical_results(self):
        """on_change should only notify when result changes.
        First check establishes baseline (no notification)."""
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult

        # Always return the same result
        mod.execute.return_value = ActionResult(
            success=True, data={"status": "up"}
        )

        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
            notify_when="on_change",
        ))
        wid = result.data["watcher_id"]

        # Wait for 3 checks
        await asyncio.sleep(16)

        w = cb._watchers[wid]
        assert w.check_count >= 3
        # First check is baseline (no notify), subsequent checks are identical → 0 notifications
        assert w.notify_count == 0

        notifications = cb.drain_bg_notifications()
        assert len(notifications) == 0

        await cb.watch_stop(WatcherIdParams(watcher_id=wid))

    @pytest.mark.asyncio
    async def test_on_change_notifies_on_result_change(self):
        """on_change should notify when result actually changes.
        First check is silent baseline."""
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult

        results = iter([
            ActionResult(success=True, data={"v": "A"}),  # check 1: baseline (silent)
            ActionResult(success=True, data={"v": "A"}),  # check 2: same → no notify
            ActionResult(success=True, data={"v": "B"}),  # check 3: A→B → notify
            ActionResult(success=True, data={"v": "B"}),  # check 4: same → no notify
            ActionResult(success=True, data={"v": "C"}),  # check 5: B→C → notify
        ])

        async def _changing_execute(action, params, **kwargs):
            return next(results, ActionResult(success=True, data={"v": "C"}))

        mod.execute = _changing_execute

        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
            notify_when="on_change",
        ))
        wid = result.data["watcher_id"]

        # Wait for 5 checks (5 * 5s = 25s, give margin)
        await asyncio.sleep(27)

        w = cb._watchers[wid]
        assert w.check_count >= 4
        # Check 1: baseline (silent), check 3: A→B, check 5: B→C = 2 notifications
        assert w.notify_count == 2

        await cb.watch_stop(WatcherIdParams(watcher_id=wid))

    @pytest.mark.asyncio
    async def test_on_error_only_notifies_on_error(self):
        """on_error should be silent when everything is fine."""
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult
        mod.execute.return_value = ActionResult(success=True, data={"ok": True})

        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
            notify_when="on_error",
        ))
        wid = result.data["watcher_id"]

        # Wait for 2 checks
        await asyncio.sleep(11)

        w = cb._watchers[wid]
        assert w.check_count >= 2
        assert w.notify_count == 0  # no errors → no notifications

        await cb.watch_stop(WatcherIdParams(watcher_id=wid))

    @pytest.mark.asyncio
    async def test_on_error_notifies_on_failure(self):
        """on_error should notify when execute raises."""
        cb, mod = _build_cb_with_module()

        call_count = 0
        async def _failing_execute(action, params):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Connection refused")
            from digitorn.modules.base import ActionResult
            return ActionResult(success=True, data={"ok": True})

        mod.execute = _failing_execute

        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
            notify_when="on_error",
        ))
        wid = result.data["watcher_id"]

        # Wait for 3 checks
        await asyncio.sleep(16)

        w = cb._watchers[wid]
        assert w.check_count >= 3
        # Check 2 errored → notify, check 3 recovered → notify
        assert w.notify_count >= 2

        await cb.watch_stop(WatcherIdParams(watcher_id=wid))

    @pytest.mark.asyncio
    async def test_on_threshold_with_real_loop(self):
        """on_threshold should notify only when expression is true."""
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult

        values = iter([180, 190, 210, 220])

        async def _threshold_execute(action, params, **kwargs):
            v = next(values, 220)
            return ActionResult(success=True, data={"cpu": v})

        mod.execute = _threshold_execute

        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
            notify_when="on_threshold",
            notify_config={"expression": "result.cpu > 200"},
        ))
        wid = result.data["watcher_id"]

        await asyncio.sleep(22)

        w = cb._watchers[wid]
        assert w.check_count >= 4
        # Only checks 3 and 4 (cpu=210, 220) should trigger
        assert w.notify_count >= 2

        await cb.watch_stop(WatcherIdParams(watcher_id=wid))

    @pytest.mark.asyncio
    async def test_summary_batches_notifications(self):
        """summary strategy should batch N checks into one notification."""
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult

        counter = 0
        async def _counting_execute(action, params):
            nonlocal counter
            counter += 1
            return ActionResult(success=True, data={"n": counter})

        mod.execute = _counting_execute

        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
            notify_when="summary",
            notify_config={"batch_size": 3},
        ))
        wid = result.data["watcher_id"]

        # Wait for 4 checks — batch fires after 3rd
        await asyncio.sleep(22)

        w = cb._watchers[wid]
        assert w.check_count >= 3
        assert w.notify_count >= 1

        notifications = cb.drain_bg_notifications()
        # At least one summary notification with batch
        summary_notifs = [n for n in notifications if n.get("summary_batch")]
        assert len(summary_notifs) >= 1
        assert len(summary_notifs[0]["summary_batch"]) == 3

        await cb.watch_stop(WatcherIdParams(watcher_id=wid))

    @pytest.mark.asyncio
    async def test_watcher_handles_execute_exception(self):
        """Watcher should not crash when execute raises — just record error."""
        cb, mod = _build_cb_with_module()

        async def _always_fail(action, params, **kwargs):
            raise RuntimeError("Boom!")

        mod.execute = _always_fail

        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
            notify_when="always",
        ))
        wid = result.data["watcher_id"]

        await asyncio.sleep(7)

        w = cb._watchers[wid]
        assert w.check_count >= 1
        assert w.last_error is not None
        assert "Boom" in w.last_error
        # Watcher is still running (not crashed)
        assert w.status == "running"

        await cb.watch_stop(WatcherIdParams(watcher_id=wid))

    @pytest.mark.asyncio
    async def test_max_checks_auto_stops(self):
        """Watcher with max_checks=2 should auto-stop after 2 checks."""
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult

        counter = 0
        async def _counting(action, params):
            nonlocal counter
            counter += 1
            return ActionResult(success=True, data={"n": counter})

        mod.execute = _counting

        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
            notify_when="always", max_checks=2,
        ))
        wid = result.data["watcher_id"]

        # Wait for 3+ intervals to confirm it stops at 2
        await asyncio.sleep(18)

        w = cb._watchers[wid]
        assert w.check_count == 2
        assert w.status == "completed"
        # Task should be done
        assert w._asyncio_task.done()

    @pytest.mark.asyncio
    async def test_max_checks_one_shot_timer(self):
        """max_checks=1 creates a one-shot delayed action."""
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult
        mod.execute.return_value = ActionResult(success=True, data={"time": "12:00"})

        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
            notify_when="always", max_checks=1,
            label="One-shot timer",
        ))
        wid = result.data["watcher_id"]

        # Wait for 1 check + margin
        await asyncio.sleep(7)

        w = cb._watchers[wid]
        assert w.check_count == 1
        assert w.notify_count == 1
        assert w.status == "completed"

        # Verify notification was queued
        notifications = cb.drain_bg_notifications()
        assert len(notifications) == 1
        assert notifications[0]["label"] == "One-shot timer"

    @pytest.mark.asyncio
    async def test_history_records_all_checks(self):
        """Each check should produce a history entry."""
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult
        mod.execute.return_value = ActionResult(success=True, data={"v": 1})

        result = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0,
            notify_when="always",
        ))
        wid = result.data["watcher_id"]

        await asyncio.sleep(11)

        hist_result = await cb.watch_history(WatchHistoryParams(
            watcher_id=wid, last_n=50,
        ))
        assert hist_result.success
        entries = hist_result.data["entries"]
        assert len(entries) >= 2
        assert all("timestamp" in e for e in entries)
        assert all("check_number" in e for e in entries)

        await cb.watch_stop(WatcherIdParams(watcher_id=wid))


# ===========================================================================
# Tests — Notification Format
# ===========================================================================


class TestNotificationFormat:

    def test_push_watcher_notification_fields(self):
        cb = ContextBuilderModule()
        w = Watcher(
            watcher_id="abc",
            tool_name="http.get",
            params={},
            interval=30,
            label="API check",
            notify_when="on_change",
            notify_config={},
            check_count=5,
            notify_count=2,
        )
        cb._push_watcher_notification(w, {"status": 200}, None)

        notifications = cb.drain_bg_notifications()
        assert len(notifications) == 1
        n = notifications[0]
        assert n["type"] == "watcher"
        assert n["watcher_id"] == "abc"
        assert n["tool_name"] == "http.get"
        assert n["label"] == "API check"
        assert n["check_number"] == 5
        assert n["strategy"] == "on_change"
        assert n["result"] == {"status": 200}
        assert "error" not in n

    def test_push_watcher_notification_with_error(self):
        cb = ContextBuilderModule()
        w = Watcher(
            watcher_id="x",
            tool_name="http.get",
            params={},
            interval=10,
            label="test",
            notify_when="always",
            notify_config={},
        )
        cb._push_watcher_notification(w, None, "Connection refused")

        notifications = cb.drain_bg_notifications()
        assert len(notifications) == 1
        assert notifications[0]["error"] == "Connection refused"
        assert "result" not in notifications[0]

    def test_push_watcher_notification_large_result_truncated(self):
        cb = ContextBuilderModule()
        w = Watcher(
            watcher_id="x",
            tool_name="http.get",
            params={},
            interval=10,
            label="test",
            notify_when="always",
            notify_config={},
        )
        large_result = "x" * 5000
        cb._push_watcher_notification(w, large_result, None)

        notifications = cb.drain_bg_notifications()
        n = notifications[0]
        assert "result_preview" in n
        assert len(n["result_preview"]) < 3000
        assert "result" not in n

    def test_summary_notification_includes_batch(self):
        cb = ContextBuilderModule()
        w = Watcher(
            watcher_id="x",
            tool_name="http.get",
            params={},
            interval=10,
            label="test",
            notify_when="summary",
            notify_config={"batch_size": 2},
        )
        w._accumulator = [
            {"check": 1, "result": {"v": 1}, "error": None},
            {"check": 2, "result": {"v": 2}, "error": None},
        ]
        cb._push_watcher_notification(w, {"v": 2}, None)

        notifications = cb.drain_bg_notifications()
        n = notifications[0]
        assert "summary_batch" in n
        assert len(n["summary_batch"]) == 2
        # Accumulator should be cleared after push
        assert w._accumulator == []


# ===========================================================================
# Tests — has_active_bg_tasks with watchers
# ===========================================================================


class TestHasActiveBgTasksWithWatchers:

    def test_no_watchers_returns_false(self):
        cb = ContextBuilderModule()
        cb._index = MagicMock()
        cb._index.tools = {}
        assert cb.has_active_bg_tasks() is False

    def test_running_watcher_returns_true(self):
        cb = ContextBuilderModule()
        cb._index = MagicMock()
        cb._index.tools = {}
        w = Watcher(
            watcher_id="x", tool_name="http.get", params={},
            interval=10, label="test", notify_when="always", notify_config={},
        )
        w.status = "running"
        cb._watchers["x"] = w
        assert cb.has_active_bg_tasks() is True

    def test_paused_watcher_returns_true(self):
        cb = ContextBuilderModule()
        cb._index = MagicMock()
        cb._index.tools = {}
        w = Watcher(
            watcher_id="x", tool_name="http.get", params={},
            interval=10, label="test", notify_when="always", notify_config={},
        )
        w.status = "paused"
        cb._watchers["x"] = w
        assert cb.has_active_bg_tasks() is True

    def test_stopped_watcher_not_active(self):
        cb = ContextBuilderModule()
        cb._index = MagicMock()
        cb._index.tools = {}
        w = Watcher(
            watcher_id="x", tool_name="http.get", params={},
            interval=10, label="test", notify_when="always", notify_config={},
        )
        w.status = "stopped"
        cb._watchers["x"] = w
        assert cb.has_active_bg_tasks() is False


# ===========================================================================
# Tests — on_stop cleanup
# ===========================================================================


class TestOnStopCleansUpWatchers:

    @pytest.mark.asyncio
    async def test_on_stop_cancels_all_watchers(self):
        cb, mod = _build_cb_with_module()
        from digitorn.modules.base import ActionResult
        mod.execute.return_value = ActionResult(success=True, data={"ok": True})

        # Start 2 watchers
        r1 = await cb.watch_start(WatchStartParams(
            name="http.get", params={}, interval=5.0, notify_when="always",
        ))
        r2 = await cb.watch_start(WatchStartParams(
            name="http.get", params={"url": "b"}, interval=5.0, notify_when="always",
        ))

        wid1 = r1.data["watcher_id"]
        wid2 = r2.data["watcher_id"]

        task1 = cb._watchers[wid1]._asyncio_task
        task2 = cb._watchers[wid2]._asyncio_task

        assert not task1.done()
        assert not task2.done()

        # Stop module
        await cb.on_stop()

        # All watchers should be cleared
        assert len(cb._watchers) == 0
        # Tasks should be cancelled
        await asyncio.sleep(0.1)
        assert task1.done()
        assert task2.done()


# ===========================================================================
# Tests — Notification formatting (agent_loop functions)
# ===========================================================================


class TestNotificationFormatting:
    """Test the formatting functions in agent_loop."""

    def test_format_watcher_notification(self):
        from digitorn.core.runtime.agent_loop import _format_watcher_notification

        notif = {
            "type": "watcher",
            "watcher_id": "abc123",
            "tool_name": "http.get",
            "label": "API health",
            "check_number": 10,
            "notify_count": 3,
            "interval": 30,
            "strategy": "on_change",
            "result": {"status": 200},
        }
        text = _format_watcher_notification(notif)
        assert "[WATCHER UPDATE]" in text
        assert "abc123" in text
        assert "http.get" in text
        assert "API health" in text
        assert "200" in text

    def test_format_watcher_notification_with_error(self):
        from digitorn.core.runtime.agent_loop import _format_watcher_notification

        notif = {
            "type": "watcher",
            "watcher_id": "x",
            "tool_name": "http.get",
            "label": "check",
            "check_number": 5,
            "notify_count": 1,
            "interval": 10,
            "strategy": "on_error",
            "error": "Connection refused",
        }
        text = _format_watcher_notification(notif)
        assert "[WATCHER UPDATE]" in text
        assert "Connection refused" in text

    def test_format_bg_task_notification(self):
        from digitorn.core.runtime.agent_loop import _format_bg_task_notification

        notif = {
            "task_id": "task123",
            "tool_name": "shell.run",
            "status": "completed",
            "elapsed_seconds": 5.2,
            "result": "output data",
        }
        text = _format_bg_task_notification(notif)
        assert "[BACKGROUND TASK COMPLETED]" in text
        assert "task123" in text
        assert "shell.run" in text


# ===========================================================================
# Tests — YAML toggle (schema + compiler)
# ===========================================================================


class TestYAMLWatcherToggle:

    def test_execution_config_watchers_default_false(self):
        from digitorn.core.app.schema import ExecutionConfig
        config = ExecutionConfig(mode="conversation")
        assert config.watchers is False

    def test_execution_config_watchers_true(self):
        from digitorn.core.app.schema import ExecutionConfig
        config = ExecutionConfig(mode="conversation", watchers=True)
        assert config.watchers is True

    def test_compiled_execution_watchers_field(self):
        from digitorn.core.app.compiler import CompiledExecution
        ce = CompiledExecution(
            mode="conversation",
            max_turns=100,
            timeout=600,
            watchers=True,
        )
        assert ce.watchers is True

    def test_compiled_execution_watchers_default(self):
        from digitorn.core.app.compiler import CompiledExecution
        ce = CompiledExecution(mode="conversation", max_turns=100, timeout=600)
        assert ce.watchers is False


# ===========================================================================
# Tests — Bootstrap primitive filtering
# ===========================================================================


class TestPrimitiveFiltering:

    def test_base_primitive_actions_exist(self):
        from digitorn.core.runtime.bootstrap import _BASE_PRIMITIVE_ACTIONS
        assert "run_parallel" in _BASE_PRIMITIVE_ACTIONS
        assert "background_run" in _BASE_PRIMITIVE_ACTIONS
        # Watchers should NOT be in base
        assert "watch_start" not in _BASE_PRIMITIVE_ACTIONS

    def test_watcher_actions_exist(self):
        from digitorn.core.runtime.bootstrap import _WATCHER_ACTIONS
        assert "watch_start" in _WATCHER_ACTIONS
        assert "watch_stop" in _WATCHER_ACTIONS
        assert "watch_pause" in _WATCHER_ACTIONS
        assert "watch_resume" in _WATCHER_ACTIONS
        assert "watch_status" in _WATCHER_ACTIONS
        assert "watch_list" in _WATCHER_ACTIONS
        assert "watch_history" in _WATCHER_ACTIONS
        assert len(_WATCHER_ACTIONS) == 7

    def test_agent_context_watchers_enabled_field(self):
        from digitorn.core.runtime.types import AgentContext
        ctx = AgentContext(
            agent_id="test",
            role="assistant",
            provider=MagicMock(),
            system_prompt="test",
            tools=[],
            watchers_enabled=True,
        )
        assert ctx.watchers_enabled is True

    def test_agent_context_watchers_default_false(self):
        from digitorn.core.runtime.types import AgentContext
        ctx = AgentContext(
            agent_id="test",
            role="assistant",
            provider=MagicMock(),
            system_prompt="test",
            tools=[],
        )
        assert ctx.watchers_enabled is False
