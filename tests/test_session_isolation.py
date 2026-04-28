"""Tests for session isolation - agents, notifications, background tasks, workbench.

Verifies that session-scoped state (agents, notification queues, background
tasks, workbench buffers) is fully isolated between sessions and that
standalone mode (no session_id) still works via the "_standalone" fallback.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from unittest.mock import MagicMock

import pytest

from digitorn.modules.agent_spawn.module import AgentSpawnModule
from digitorn.modules.agent_spawn.runner import AgentResult, TrackedAgent
from digitorn.modules.base import ExecutionContext
from digitorn.modules.context_builder.module import ContextBuilderModule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_exec_context(session_id: str | None = None) -> ExecutionContext:
    return ExecutionContext(plan_id="test", action_id="test", session_id=session_id)


def _set_context_var(module, ctx: ExecutionContext | None):
    """Simulate the runtime setting _context_var for a module."""
    module._context_var = ContextVar(f"ctx_{id(module)}", default=None)
    if ctx is not None:
        module._context_var.set(ctx)


# ---------------------------------------------------------------------------
# Agent Spawn - session isolation
# ---------------------------------------------------------------------------


class TestAgentSpawnSessionIsolation:
    """Agents must be scoped per-session."""

    def setup_method(self):
        self.module = AgentSpawnModule()
        self.module._coordinator_provider = MagicMock()

    def _with_session(self, session_id: str | None):
        _set_context_var(self.module, _make_exec_context(session_id))

    def _add_fake_agent(self, session_id: str, agent_id: str, status: str = "running"):
        """Insert a fake tracked agent for testing."""
        tracked = TrackedAgent(
            agent_id=agent_id,
            task="fake task",
            specialist=None,
        )
        if status != "running":
            tracked.result = AgentResult(
                agent_id=agent_id, task="fake task", status=status,
            )
        else:
            # Create a never-finishing task
            tracked.asyncio_task = asyncio.Future()
        self.module._agents.setdefault(session_id, {})[agent_id] = tracked

    def test_session_agents_isolated(self):
        """Session A's agents are invisible to Session B."""
        self._add_fake_agent("session_a", "agent_001")
        self._add_fake_agent("session_b", "agent_002")

        self._with_session("session_a")
        agents_a = self.module._session_agents()
        assert "agent_001" in agents_a
        assert "agent_002" not in agents_a

        self._with_session("session_b")
        agents_b = self.module._session_agents()
        assert "agent_002" in agents_b
        assert "agent_001" not in agents_b

    def test_total_running_counts_all_sessions(self):
        """Pool limit counts across all sessions (app-scoped resource)."""
        self._add_fake_agent("session_a", "agent_001")
        self._add_fake_agent("session_b", "agent_002")
        assert self.module._total_running() == 2

    @pytest.mark.asyncio
    async def test_agent_status_cross_session_denied(self):
        """Session A cannot check Session B's agent."""
        self._add_fake_agent("session_a", "agent_001")

        self._with_session("session_b")
        from digitorn.modules.agent_spawn.params import AgentStatusParams
        result = await self.module.agent_status(AgentStatusParams(agent_id="agent_001"))
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_agent_cancel_cross_session_denied(self):
        """Session A cannot cancel Session B's agent."""
        self._add_fake_agent("session_a", "agent_001")

        self._with_session("session_b")
        from digitorn.modules.agent_spawn.params import AgentCancelParams
        result = await self.module.agent_cancel(AgentCancelParams(agent_id="agent_001"))
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_agent_list_only_own_session(self):
        """agent_list only returns agents from the current session."""
        self._add_fake_agent("session_a", "agent_001")
        self._add_fake_agent("session_a", "agent_003")
        self._add_fake_agent("session_b", "agent_002")

        self._with_session("session_a")
        from digitorn.modules.agent_spawn.params import AgentListParams
        result = await self.module.agent_list(AgentListParams())
        assert result.success
        agent_ids = [a["agent_id"] for a in result.data["agents"]]
        assert "agent_001" in agent_ids
        assert "agent_003" in agent_ids
        assert "agent_002" not in agent_ids

    @pytest.mark.asyncio
    async def test_cleanup_session(self):
        """cleanup_session cancels and removes all agents for that session."""
        self._add_fake_agent("session_a", "agent_001")
        self._add_fake_agent("session_a", "agent_002")
        self._add_fake_agent("session_b", "agent_003")

        await self.module.cleanup_session("session_a")

        assert "session_a" not in self.module._agents
        assert "agent_003" in self.module._agents.get("session_b", {})

    def test_standalone_fallback(self):
        """No session_id falls back to '_standalone'."""
        _set_context_var(self.module, _make_exec_context(None))
        assert self.module._session_id() == "_standalone"

    @pytest.mark.asyncio
    async def test_on_stop_cleans_all_sessions(self):
        """on_stop cancels agents across all sessions."""
        self._add_fake_agent("session_a", "agent_001")
        self._add_fake_agent("session_b", "agent_002")

        await self.module.on_stop()

        assert len(self.module._agents) == 0


# ---------------------------------------------------------------------------
# Notification queue - session isolation
# ---------------------------------------------------------------------------


class TestNotificationQueueIsolation:
    """Notification queues must be per-session."""

    def setup_method(self):
        self.cb = ContextBuilderModule()
        _set_context_var(self.cb, _make_exec_context(None))

    def test_push_routes_by_session_id_in_notification(self):
        """Notifications with session_id go to the correct queue."""
        self.cb.push_module_notification({"type": "test", "session_id": "sess_a"})
        self.cb.push_module_notification({"type": "test", "session_id": "sess_b"})
        self.cb.push_module_notification({"type": "test2", "session_id": "sess_a"})

        a_notifs = self.cb.drain_bg_notifications(session_id="sess_a")
        b_notifs = self.cb.drain_bg_notifications(session_id="sess_b")

        assert len(a_notifs) == 2
        assert len(b_notifs) == 1
        assert a_notifs[0]["type"] == "test"
        assert a_notifs[1]["type"] == "test2"

    def test_drain_empty_session_returns_empty(self):
        """Draining a non-existent session returns empty list."""
        result = self.cb.drain_bg_notifications(session_id="nonexistent")
        assert result == []

    def test_cleanup_session_queue(self):
        """cleanup_session_queue removes the session's queue."""
        self.cb.push_module_notification({"type": "test", "session_id": "sess_a"})
        self.cb.cleanup_session_queue("sess_a")

        result = self.cb.drain_bg_notifications(session_id="sess_a")
        assert result == []

    def test_standalone_fallback(self):
        """No session_id in notification or context → _standalone queue."""
        _set_context_var(self.cb, _make_exec_context(None))
        self.cb._session_id = None
        self.cb.push_module_notification({"type": "standalone_test"})

        result = self.cb.drain_bg_notifications(session_id="_standalone")
        assert len(result) == 1
        assert result[0]["type"] == "standalone_test"


# ---------------------------------------------------------------------------
# Background tasks - session isolation
# ---------------------------------------------------------------------------


class TestBackgroundTasksIsolation:
    """Background tasks must be scoped per-session."""

    def setup_method(self):
        self.cb = ContextBuilderModule()

    def test_session_bg_tasks_isolated(self):
        """Each session has its own task dict."""
        _set_context_var(self.cb, _make_exec_context("sess_a"))
        tasks_a = self.cb._session_bg_tasks()
        tasks_a["task_001"] = MagicMock()

        _set_context_var(self.cb, _make_exec_context("sess_b"))
        tasks_b = self.cb._session_bg_tasks()

        assert "task_001" not in tasks_b
        assert "task_001" in self.cb._session_bg_tasks("sess_a")

    @pytest.mark.asyncio
    async def test_cleanup_session_bg_tasks(self):
        """cleanup_session_bg_tasks removes all tasks for a session."""
        _set_context_var(self.cb, _make_exec_context("sess_a"))
        mock_task = MagicMock()
        mock_task._asyncio_task = asyncio.Future()
        mock_task._asyncio_task.done = MagicMock(return_value=False)
        mock_task._asyncio_task.cancel = MagicMock()
        self.cb._session_bg_tasks()["task_001"] = mock_task

        await self.cb.cleanup_session_bg_tasks("sess_a")

        assert "sess_a" not in self.cb._background_tasks


# ---------------------------------------------------------------------------
# Workbench - session isolation
# ---------------------------------------------------------------------------


class TestWorkbenchIsolation:
    """Workbench instances must be per-session."""

    def setup_method(self):
        self.cb = ContextBuilderModule()
        self.cb._wb_enabled = True
        self.cb._wb_max_buffers = 10
        self.cb._wb_max_chars = 200_000
        self.cb._wb_max_snapshots = 5

    def test_different_sessions_get_different_workbenches(self):
        """Each session gets its own Workbench instance."""
        _set_context_var(self.cb, _make_exec_context("sess_a"))
        wb_a = self.cb._get_workbench()

        _set_context_var(self.cb, _make_exec_context("sess_b"))
        wb_b = self.cb._get_workbench()

        assert wb_a is not wb_b

    def test_same_session_gets_same_workbench(self):
        """Same session always gets the same instance."""
        _set_context_var(self.cb, _make_exec_context("sess_a"))
        wb1 = self.cb._get_workbench()
        wb2 = self.cb._get_workbench()
        assert wb1 is wb2

    def test_explicit_session_id_param(self):
        """Passing session_id explicitly overrides context."""
        _set_context_var(self.cb, _make_exec_context("sess_a"))
        wb_b = self.cb._get_workbench(session_id="sess_b")

        # Should be different from session A's workbench
        wb_a = self.cb._get_workbench(session_id="sess_a")
        assert wb_a is not wb_b

    def test_cleanup_session_workbench(self):
        """cleanup_session_workbench removes the session's workbench."""
        _set_context_var(self.cb, _make_exec_context("sess_a"))
        wb1 = self.cb._get_workbench()
        self.cb.cleanup_session_workbench("sess_a")

        # Getting it again should create a new instance
        wb2 = self.cb._get_workbench()
        assert wb1 is not wb2

    def test_writes_dont_leak_across_sessions(self):
        """Writing to session A's workbench doesn't affect session B."""
        _set_context_var(self.cb, _make_exec_context("sess_a"))
        wb_a = self.cb._get_workbench()
        wb_a.write("report", "Hello from A")

        _set_context_var(self.cb, _make_exec_context("sess_b"))
        wb_b = self.cb._get_workbench()

        assert wb_a.buffer_count == 1
        assert wb_b.buffer_count == 0

    def test_standalone_fallback(self):
        """No session_id → '_standalone' key."""
        _set_context_var(self.cb, _make_exec_context(None))
        wb = self.cb._get_workbench()
        assert wb is self.cb._get_workbench(session_id="_standalone")
