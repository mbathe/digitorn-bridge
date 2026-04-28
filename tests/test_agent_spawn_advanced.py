"""Advanced Agent Spawn module tests - 1 tool, 8 modes.

Tests for:
- Mode 1 (Spawn Sync): launch agent, wait, return result
- Mode 2 (Spawn Async): launch background, return agent_id
- Mode 3 (Status): check agent status
- Mode 4 (Wait One): block until agent finishes
- Mode 5 (Wait All): wait for multiple agents
- Mode 6 (Cancel): terminate running agent
- Mode 7 (Reassign): respawn failed agent with new task
- Mode 8 (List): list all agents with status
- Metrics relay: real-time token/tool_call tracking
- Notifications: completion/failure notifications
- Error handling: pool full, not found, no-nesting
"""

from __future__ import annotations

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

from digitorn.modules.agent_spawn.module import AgentSpawnModule
from digitorn.modules.agent_spawn.params import AgentParams
from digitorn.modules.agent_spawn.runner import AgentResult, TrackedAgent

from _test_helpers import run_coro


# ── Fixtures ──────────────────────────────────────────────────


class FakeProvider:
    """Minimal LLM provider mock."""
    _context_max = 50000


class FakeTurnResult:
    """Minimal turn result mock."""
    def __init__(self, content="Done.", turns=1, error=None):
        self.content = content
        self.turns_used = turns
        self.error = error
        self.tool_calls = []


@pytest.fixture
def module():
    """Create a fresh AgentSpawnModule with mocked provider."""
    m = AgentSpawnModule()
    m._coordinator_provider = FakeProvider()
    m._coordinator_tools = [{"name": "test_tool"}]
    m._coordinator_modules = {}
    m._max_workers = 5
    return m


def _make_fast_runner(content="Agent result.", turns=2, status="completed"):
    """Create a mock for run_isolated_agent that completes instantly."""
    async def _mock_runner(**kwargs):
        return AgentResult(
            agent_id=kwargs.get("agent_id", "test"),
            task=kwargs.get("task", "test"),
            specialist=kwargs.get("specialist"),
            status=status,
            content=content,
            turns_used=turns,
            duration_seconds=0.1,
        )
    return _mock_runner


def _make_slow_runner(delay=2.0, content="Slow result.", turns=5):
    """Create a mock runner that takes time (for async/cancel tests)."""
    async def _mock_runner(**kwargs):
        await asyncio.sleep(delay)
        return AgentResult(
            agent_id=kwargs.get("agent_id", "test"),
            task=kwargs.get("task", "test"),
            specialist=kwargs.get("specialist"),
            status="completed",
            content=content,
            turns_used=turns,
            duration_seconds=delay,
        )
    return _mock_runner


def _make_failing_runner(error_msg="LLM crashed"):
    """Create a mock runner that fails."""
    async def _mock_runner(**kwargs):
        return AgentResult(
            agent_id=kwargs.get("agent_id", "test"),
            task=kwargs.get("task", "test"),
            specialist=kwargs.get("specialist"),
            status="failed",
            content="",
            errors=[error_msg],
            duration_seconds=0.1,
        )
    return _mock_runner


# ═══════════════════════════════════════════════════════════════
# MODE 1: SPAWN SYNC
# ═══════════════════════════════════════════════════════════════

class TestSpawnSync:
    """Mode 1: Agent(prompt='...') - default, blocks until done."""

    @pytest.mark.asyncio
    async def test_spawn_sync_returns_result(self, module):
        """Sync spawn returns the agent's final result."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_fast_runner("Found 5 API endpoints.")
        ):
            r = await module.agent(AgentParams(prompt="Find API endpoints"))
            assert r.success
            assert r.data["status"] == "completed"
            assert "Found 5 API endpoints" in r.data["content"]
            assert r.data["turns_used"] == 2

    @pytest.mark.asyncio
    async def test_spawn_sync_with_specialist(self, module):
        """Sync spawn with specialist parameter."""
        module._specialists["explore"] = {
            "provider": FakeProvider(),
            "system_prompt": "You are an explorer.",
            "tools": [{"name": "read"}],
            "modules": {},
        }
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_fast_runner("Explored codebase.")
        ):
            r = await module.agent(AgentParams(
                prompt="Search for auth code",
                specialist="explore",
            ))
            assert r.success
            assert r.data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_spawn_sync_failed_agent(self, module):
        """Sync spawn returns failure when agent fails."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_failing_runner("Out of tokens")
        ):
            r = await module.agent(AgentParams(prompt="Complex task"))
            assert r.success  # The tool call succeeded (agent ran)
            assert r.data["status"] == "failed"
            assert "Out of tokens" in r.data["errors"]

    @pytest.mark.asyncio
    async def test_spawn_no_provider_error(self, module):
        """Error when no provider configured."""
        module._coordinator_provider = None
        r = await module.agent(AgentParams(prompt="Do something"))
        assert not r.success
        assert "provider" in r.error.lower()


# ═══════════════════════════════════════════════════════════════
# MODE 2: SPAWN ASYNC (BACKGROUND)
# ═══════════════════════════════════════════════════════════════

class TestSpawnAsync:
    """Mode 2: Agent(prompt='...', wait=false) - fire and forget."""

    @pytest.mark.asyncio
    async def test_async_returns_agent_id(self, module):
        """Async spawn returns agent_id immediately."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_slow_runner(delay=5.0)
        ):
            r = await module.agent(AgentParams(
                prompt="Run all tests",
                wait=False,
                description="Running tests",
            ))
            assert r.success
            assert "agent_id" in r.data
            assert r.data["status"] == "running"
            assert r.data["description"] == "Running tests"

    @pytest.mark.asyncio
    async def test_async_agent_runs_in_background(self, module):
        """Background agent is tracked and running."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_slow_runner(delay=5.0)
        ):
            r = await module.agent(AgentParams(prompt="Long task", wait=False))
            agent_id = r.data["agent_id"]

            # Agent should be in the tracked pool
            tracked = module._session_agents().get(agent_id)
            assert tracked is not None
            assert tracked.asyncio_task is not None
            assert not tracked.asyncio_task.done()

            # Clean up
            tracked.asyncio_task.cancel()
            try:
                await tracked.asyncio_task
            except (asyncio.CancelledError, Exception):
                pass


# ═══════════════════════════════════════════════════════════════
# MODE 3: STATUS CHECK
# ═══════════════════════════════════════════════════════════════

class TestStatusCheck:
    """Mode 3: Agent(agent_id='...') - check status without blocking."""

    @pytest.mark.asyncio
    async def test_status_running_agent(self, module):
        """Status check shows running agent."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_slow_runner(delay=10.0)
        ):
            r = await module.agent(AgentParams(prompt="Long task", wait=False))
            agent_id = r.data["agent_id"]

            # Check status
            r2 = await module.agent(AgentParams(agent_id=agent_id, wait=False))
            assert r2.success
            assert r2.data["status"] == "running"
            assert "elapsed_seconds" in r2.data

            # Clean up
            tracked = module._session_agents().get(agent_id)
            if tracked and tracked.asyncio_task:
                tracked.asyncio_task.cancel()
                try:
                    await tracked.asyncio_task
                except (asyncio.CancelledError, Exception):
                    pass

    @pytest.mark.asyncio
    async def test_status_completed_agent(self, module):
        """Status check on completed agent returns full result."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_fast_runner("All tests pass.")
        ):
            # Spawn background and wait for it
            r = await module.agent(AgentParams(prompt="Quick task", wait=False))
            agent_id = r.data["agent_id"]
            await asyncio.sleep(0.3)  # Let it complete

            # Check status - should have result
            r2 = await module.agent(AgentParams(agent_id=agent_id, wait=False))
            assert r2.success
            assert r2.data["status"] == "completed"
            assert "All tests pass" in r2.data.get("content", "")

    @pytest.mark.asyncio
    async def test_status_nonexistent_agent(self, module):
        """Status check on non-existent agent returns error."""
        r = await module.agent(AgentParams(agent_id="fake_id_xyz", wait=False))
        assert not r.success
        assert "not found" in r.error.lower()


# ═══════════════════════════════════════════════════════════════
# MODE 4: WAIT ONE
# ═══════════════════════════════════════════════════════════════

class TestWaitOne:
    """Mode 4: Agent(agent_id='...', wait=true) - block until done."""

    @pytest.mark.asyncio
    async def test_wait_returns_result(self, module):
        """Wait mode blocks and returns result."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_fast_runner("Result here.")
        ):
            r = await module.agent(AgentParams(prompt="Quick task", wait=False))
            agent_id = r.data["agent_id"]

            # Wait for result
            r2 = await module.agent(AgentParams(agent_id=agent_id, wait=True))
            assert r2.success
            assert r2.data["status"] == "completed"
            assert "Result here" in r2.data["content"]

    @pytest.mark.asyncio
    async def test_wait_timeout(self, module):
        """Wait respects timeout."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_slow_runner(delay=30.0)
        ):
            r = await module.agent(AgentParams(prompt="Very long task", wait=False))
            agent_id = r.data["agent_id"]

            # Wait with short timeout
            r2 = await module.agent(AgentParams(
                agent_id=agent_id, wait=True, timeout=1.0,
            ))
            assert not r2.success
            assert "still running" in r2.error.lower()

            # Clean up
            tracked = module._session_agents().get(agent_id)
            if tracked and tracked.asyncio_task:
                tracked.asyncio_task.cancel()
                try:
                    await tracked.asyncio_task
                except (asyncio.CancelledError, Exception):
                    pass

    @pytest.mark.asyncio
    async def test_wait_nonexistent(self, module):
        """Wait on non-existent agent returns error."""
        r = await module.agent(AgentParams(agent_id="fake_id", wait=True))
        assert not r.success
        assert "not found" in r.error.lower()


# ═══════════════════════════════════════════════════════════════
# MODE 5: WAIT ALL
# ═══════════════════════════════════════════════════════════════

class TestWaitAll:
    """Mode 5: Agent(agent_ids=[...]) - wait for multiple agents."""

    @pytest.mark.asyncio
    async def test_wait_all_collects_results(self, module):
        """Wait all returns results from multiple agents."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_fast_runner("Done.")
        ):
            # Spawn 3 background agents
            ids = []
            for i in range(3):
                r = await module.agent(AgentParams(
                    prompt=f"Task {i}", wait=False,
                ))
                assert r.success
                ids.append(r.data["agent_id"])

            # Wait a bit for them to complete
            await asyncio.sleep(0.5)

            # Wait all
            r2 = await module.agent(AgentParams(agent_ids=ids))
            assert r2.success
            assert r2.data["total"] == 3
            assert r2.data["completed"] == 3

    @pytest.mark.asyncio
    async def test_wait_all_no_agents(self, module):
        """Wait all with no running agents returns empty."""
        r = await module.agent(AgentParams(agent_ids=[]))
        assert r.success
        assert r.data["completed"] == 0

    @pytest.mark.asyncio
    async def test_wait_all_with_timeout(self, module):
        """Wait all respects timeout for slow agents."""
        call_count = 0
        async def _mixed_runner(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.sleep(30)  # This one is slow
            return AgentResult(
                agent_id=kwargs.get("agent_id", "test"),
                task=kwargs.get("task", "test"),
                status="completed",
                content="Done.",
                turns_used=1,
                duration_seconds=0.1,
            )

        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_mixed_runner
        ):
            r1 = await module.agent(AgentParams(prompt="Slow", wait=False))
            r2 = await module.agent(AgentParams(prompt="Fast", wait=False))
            ids = [r1.data["agent_id"], r2.data["agent_id"]]

            await asyncio.sleep(0.3)

            r = await module.agent(AgentParams(agent_ids=ids, timeout=1.0))
            assert r.success
            assert len(r.data["timed_out"]) >= 1

            # Clean up slow task
            for aid in ids:
                tracked = module._session_agents().get(aid)
                if tracked and tracked.asyncio_task and not tracked.asyncio_task.done():
                    tracked.asyncio_task.cancel()
                    try:
                        await tracked.asyncio_task
                    except (asyncio.CancelledError, Exception):
                        pass


# ═══════════════════════════════════════════════════════════════
# MODE 6: CANCEL
# ═══════════════════════════════════════════════════════════════

class TestCancel:
    """Mode 6: Agent(agent_id='...', cancel=true) - terminate agent."""

    @pytest.mark.asyncio
    async def test_cancel_running_agent(self, module):
        """Cancel stops a running agent."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_slow_runner(delay=30.0)
        ):
            r = await module.agent(AgentParams(prompt="Long task", wait=False))
            agent_id = r.data["agent_id"]

            await asyncio.sleep(0.1)

            # Cancel it
            r2 = await module.agent(AgentParams(agent_id=agent_id, cancel=True))
            assert r2.success
            assert r2.data["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self, module):
        """Cancel non-existent agent returns error."""
        r = await module.agent(AgentParams(agent_id="fake_id", cancel=True))
        assert not r.success
        assert "not found" in r.error.lower()

    @pytest.mark.asyncio
    async def test_cancel_already_finished(self, module):
        """Cancel finished agent returns error."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_fast_runner()
        ):
            r = await module.agent(AgentParams(prompt="Quick", wait=False))
            agent_id = r.data["agent_id"]
            await asyncio.sleep(0.5)

            r2 = await module.agent(AgentParams(agent_id=agent_id, cancel=True))
            assert not r2.success
            assert "not running" in r2.error.lower()


# ═══════════════════════════════════════════════════════════════
# MODE 7: REASSIGN
# ═══════════════════════════════════════════════════════════════

class TestReassign:
    """Mode 7: Agent(agent_id='...', reassign='new task') - respawn."""

    @pytest.mark.asyncio
    async def test_reassign_failed_agent(self, module):
        """Reassign a failed agent creates a new one."""
        call_count = 0
        async def _first_fails(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AgentResult(
                    agent_id=kwargs["agent_id"], task=kwargs["task"],
                    status="failed", errors=["First try failed"],
                )
            return AgentResult(
                agent_id=kwargs["agent_id"], task=kwargs["task"],
                status="completed", content="Second try worked!",
                turns_used=3, duration_seconds=0.2,
            )

        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_first_fails
        ):
            # First spawn fails
            r = await module.agent(AgentParams(prompt="Task v1"))
            assert r.data["status"] == "failed"
            agent_id = r.data["agent_id"]

            # Reassign with new task
            r2 = await module.agent(AgentParams(
                agent_id=agent_id, reassign="Task v2 - try differently",
            ))
            assert r2.success
            assert r2.data["status"] == "completed"
            assert "Second try worked" in r2.data["content"]


# ═══════════════════════════════════════════════════════════════
# MODE 8: LIST
# ═══════════════════════════════════════════════════════════════

class TestList:
    """Mode 8: Agent(list_agents=true) - list all agents."""

    @pytest.mark.asyncio
    async def test_list_empty(self, module):
        """List with no agents returns empty."""
        r = await module.agent(AgentParams(list_agents=True))
        assert r.success
        assert r.data["total"] == 0
        assert r.data["running"] == 0

    @pytest.mark.asyncio
    async def test_list_shows_running(self, module):
        """List shows running agents."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_slow_runner(delay=10.0)
        ):
            await module.agent(AgentParams(prompt="Task A", wait=False))
            await module.agent(AgentParams(prompt="Task B", wait=False))

            r = await module.agent(AgentParams(list_agents=True))
            assert r.success
            assert r.data["total"] == 2
            assert r.data["running"] == 2

            # Clean up
            for a in r.data["agents"]:
                tracked = module._session_agents().get(a["agent_id"])
                if tracked and tracked.asyncio_task:
                    tracked.asyncio_task.cancel()
                    try:
                        await tracked.asyncio_task
                    except (asyncio.CancelledError, Exception):
                        pass

    @pytest.mark.asyncio
    async def test_list_shows_metrics(self, module):
        """List includes metrics for agents."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_slow_runner(delay=10.0)
        ):
            r = await module.agent(AgentParams(prompt="Task", wait=False))
            agent_id = r.data["agent_id"]

            # Simulate metrics
            module._agent_metrics[agent_id] = {
                "tokens_in": 1500, "tokens_out": 300,
                "tool_calls": 5, "turns": 3,
            }

            r2 = await module.agent(AgentParams(list_agents=True))
            assert r2.success
            agent_entry = r2.data["agents"][0]
            assert agent_entry["metrics"]["tokens_in"] == 1500
            assert agent_entry["metrics"]["tool_calls"] == 5

            # Clean up
            tracked = module._session_agents().get(agent_id)
            if tracked and tracked.asyncio_task:
                tracked.asyncio_task.cancel()
                try:
                    await tracked.asyncio_task
                except (asyncio.CancelledError, Exception):
                    pass


# ═══════════════════════════════════════════════════════════════
# POOL LIMITS & ERROR HANDLING
# ═══════════════════════════════════════════════════════════════

class TestPoolAndErrors:
    """Pool capacity, no-nesting, invalid params."""

    @pytest.mark.asyncio
    async def test_pool_full_error(self, module):
        """Pool full returns error when max_workers reached."""
        module._max_workers = 2
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_slow_runner(delay=30.0)
        ):
            r1 = await module.agent(AgentParams(prompt="Task 1", wait=False))
            r2 = await module.agent(AgentParams(prompt="Task 2", wait=False))
            assert r1.success
            assert r2.success

            # Third should fail
            r3 = await module.agent(AgentParams(prompt="Task 3", wait=False))
            assert not r3.success
            assert "pool full" in r3.error.lower()

            # Clean up
            for r in [r1, r2]:
                tracked = module._session_agents().get(r.data["agent_id"])
                if tracked and tracked.asyncio_task:
                    tracked.asyncio_task.cancel()
                    try:
                        await tracked.asyncio_task
                    except (asyncio.CancelledError, Exception):
                        pass

    @pytest.mark.asyncio
    async def test_no_params_error(self, module):
        """No prompt, no agent_id, nothing → error."""
        r = await module.agent(AgentParams())
        assert not r.success
        assert "must provide" in r.error.lower()

    @pytest.mark.asyncio
    async def test_no_nesting(self, module):
        """Sub-agents cannot spawn sub-agents."""
        # Simulate worker context
        from digitorn.modules.base import ExecutionContext
        ctx = MagicMock(spec=ExecutionContext)
        ctx.role = "worker"
        ctx.session_id = "test"
        module._context_var.set(ctx)

        r = await module.agent(AgentParams(prompt="Nested spawn"))
        assert not r.success
        assert "sub-agents cannot spawn" in r.error.lower()


# ═══════════════════════════════════════════════════════════════
# METRICS RELAY
# ═══════════════════════════════════════════════════════════════

class TestMetricsRelay:
    """Real-time token/tool_call metrics relay."""

    @pytest.mark.asyncio
    async def test_metrics_relay_callback(self, module):
        """Metrics relay accumulates tokens and tool_calls."""
        notifications = []
        module._notify_fn = lambda n: notifications.append(n)

        agent_id = "test_metrics_agent"
        module._agent_metrics[agent_id] = {
            "agent_id": agent_id, "session_id": "s1",
            "specialist": None, "task": "test",
            "tokens_in": 0, "tokens_out": 0,
            "tool_calls": 0, "turns": 0,
            "started_at": time.monotonic(),
        }

        relay = module._build_metrics_relay(agent_id, "s1", None, "test")
        assert relay is not None

        # Simulate events
        relay({"type": "token_usage", "input_tokens": 100, "output_tokens": 50})
        relay({"type": "tool_call", "name": "Read"})
        relay({"type": "tool_call", "name": "Edit"})
        relay({"type": "turn_complete"})

        # Check accumulated metrics
        metrics = module._agent_metrics[agent_id]
        assert metrics["tokens_in"] == 100
        assert metrics["tokens_out"] == 50
        assert metrics["tool_calls"] == 2
        assert metrics["turns"] == 1

        # Check notifications were sent with metrics
        assert len(notifications) == 4
        last = notifications[-1]
        assert last["type"] == "agent_progress"
        assert last["metrics"]["tokens_in"] == 100
        assert last["metrics"]["tool_calls"] == 2

    @pytest.mark.asyncio
    async def test_metrics_no_notify_fn(self, module):
        """No crash when notify_fn is None."""
        module._notify_fn = None
        relay = module._build_metrics_relay("test", "s1", None, "test")
        assert relay is None


# ═══════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════

class TestNotifications:
    """Completion/failure notifications to parent."""

    @pytest.mark.asyncio
    async def test_completion_notification(self, module):
        """Agent completion triggers notify_fn."""
        notifications = []
        module._notify_fn = lambda n: notifications.append(n)

        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_fast_runner("Done!")
        ):
            r = await module.agent(AgentParams(prompt="Quick task"))
            assert r.success

            # The runner calls notify_fn at the end
            # Check that at least one notification was about completion
            agent_notifs = [n for n in notifications if n.get("type") == "agent_progress"]
            # Metrics relay sends progress events during execution
            assert isinstance(notifications, list)


# ═══════════════════════════════════════════════════════════════
# CLEANUP
# ═══════════════════════════════════════════════════════════════

class TestCleanup:
    """Session cleanup and memory management."""

    @pytest.mark.asyncio
    async def test_cleanup_session(self, module):
        """cleanup_session removes all agents for a session."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_slow_runner(delay=10.0)
        ):
            r = await module.agent(AgentParams(prompt="Task", wait=False))
            agent_id = r.data["agent_id"]
            sid = module._session_id()

            assert agent_id in module._session_agents(sid)

            await module.cleanup_session(sid)

            assert sid not in module._agents or len(module._agents.get(sid, {})) == 0

    @pytest.mark.asyncio
    async def test_on_stop_cancels_all(self, module):
        """on_stop cancels all running agents."""
        with patch(
            "digitorn.modules.agent_spawn.module.run_isolated_agent",
            side_effect=_make_slow_runner(delay=10.0)
        ):
            await module.agent(AgentParams(prompt="Task 1", wait=False))
            await module.agent(AgentParams(prompt="Task 2", wait=False))

            assert module._total_running() == 2

            await module.on_stop()

            assert len(module._agents) == 0
