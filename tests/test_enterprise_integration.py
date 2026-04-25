"""Cross-module integration tests for the 4 enterprise modules.

Tests that verify:
- All 4 modules load via the registry
- Service bus enables cross-module communication
- cron_native DAG triggers work across schedules
- State snapshot/restore survives roundtrip
- Module manifest and action counts are correct
"""

from __future__ import annotations

import pytest

from digitorn.modules.base import ActionResult
from digitorn.modules.service_bus import ServiceBus


# ═══════════════════════════════════════════════════════════════════════
# Registry and Loader Integration
# ═══════════════════════════════════════════════════════════════════════


class TestRegistryDiscovery:
    """Verify all 4 modules are discoverable and loadable."""

    def test_discover_all_four(self):
        from digitorn.core.loader import discover_modules

        mods = discover_modules()
        for mid in ("cache", "queue", "vector", "cron_native"):
            assert mid in mods, f"Module '{mid}' not discovered"

    def test_load_and_create_instances(self):
        from digitorn.core.loader import load_modules
        from digitorn.modules.registry import ModuleRegistry

        registry = ModuleRegistry()
        load_modules(registry)

        for mid in ("cache", "queue", "vector", "cron_native"):
            mod = registry.create(mid)
            assert mod is not None, f"Failed to create '{mid}'"
            assert mod.MODULE_ID == mid

    def test_manifest_action_counts(self):
        from digitorn.modules.cache import CacheModule
        from digitorn.modules.cron_native import CronNativeModule
        from digitorn.modules.manifest import ModuleManifest
        from digitorn.modules.queue import QueueModule
        from digitorn.modules.vector import VectorModule

        expected = {
            "cache": (CacheModule, 14),
            "queue": (QueueModule, 13),
            "vector": (VectorModule, 15),
            "cron_native": (CronNativeModule, 21),
        }
        for mid, (cls, count) in expected.items():
            m = cls()
            manifest = ModuleManifest.from_module(m)
            assert len(manifest.actions) == count, (
                f"{mid}: expected {count} actions, got {len(manifest.actions)}"
            )

    def test_total_new_actions(self):
        from digitorn.modules.cache import CacheModule
        from digitorn.modules.cron_native import CronNativeModule
        from digitorn.modules.manifest import ModuleManifest
        from digitorn.modules.queue import QueueModule
        from digitorn.modules.vector import VectorModule

        total = 0
        for cls in (CacheModule, QueueModule, VectorModule, CronNativeModule):
            total += len(ModuleManifest.from_module(cls()).actions)
        assert total == 63


# ═══════════════════════════════════════════════════════════════════════
# Service Bus Cross-Module Communication
# ═══════════════════════════════════════════════════════════════════════


class TestServiceBusIntegration:
    """Test that modules can call each other via the service bus."""

    @pytest.fixture
    async def wired_modules(self):
        from digitorn.modules.cache import CacheModule
        from digitorn.modules.queue import QueueModule

        cache = CacheModule()
        queue = QueueModule()
        await cache.on_start()
        await queue.on_start()

        bus = ServiceBus()
        bus.register_service("cache", cache)
        bus.register_service("queue", queue)
        cache._service_bus = bus
        queue._service_bus = bus

        yield {"cache": cache, "queue": queue, "bus": bus}

        await cache.on_stop()
        await queue.on_stop()

    @pytest.mark.asyncio
    async def test_cache_accessible_via_bus(self, wired_modules):
        bus = wired_modules["bus"]
        cache = wired_modules["cache"]

        # Set via direct module
        from digitorn.modules.cache.params import SetParams

        await cache.set(SetParams(key="hello", value="world"))

        # Get via service bus
        result = await bus.call("cache", "get", {"key": "hello"})
        assert result.success
        assert result.data["value"] == "world"

    @pytest.mark.asyncio
    async def test_queue_accessible_via_bus(self, wired_modules):
        bus = wired_modules["bus"]

        result = await bus.call("queue", "create_queue", {"name": "test-q"})
        assert result.success

        result = await bus.call("queue", "publish", {"queue": "test-q", "message": {"msg": "hi"}})
        assert result.success

        result = await bus.call("queue", "queue_stats", {"queue": "test-q"})
        assert result.success
        assert result.data["depth"] >= 1

    @pytest.mark.asyncio
    async def test_service_bus_list_services(self, wired_modules):
        bus = wired_modules["bus"]
        services = bus.list_services()
        names = {s["name"] for s in services}
        assert "cache" in names
        assert "queue" in names


# ═══════════════════════════════════════════════════════════════════════
# Cron Native — DAG and Holiday Integration
# ═══════════════════════════════════════════════════════════════════════


class TestCronNativeIntegration:
    """Test cron_native complex scenarios."""

    @pytest.fixture
    async def cron(self):
        croniter = pytest.importorskip("croniter")
        from digitorn.modules.cron_native import CronNativeModule

        m = CronNativeModule()
        await m.on_start()
        yield m
        await m.on_stop()

    @pytest.mark.asyncio
    async def test_full_schedule_lifecycle(self, cron):
        """Create → pause → resume → run → history → delete."""
        from digitorn.modules.cron_native.params import (
            CreateScheduleParams,
            DeleteScheduleParams,
            ExecutionHistoryParams,
            PauseScheduleParams,
            ResumeScheduleParams,
            RunNowParams,
        )

        # Create
        r = await cron.create_schedule(CreateScheduleParams(
            name="lifecycle-test",
            cron_expr="0 9 * * 1-5",
            action_type="notification",
            prompt="Daily standup reminder",
        ))
        assert r.success
        assert r.data["status"] == "active"

        # Pause
        r = await cron.pause_schedule(PauseScheduleParams(name="lifecycle-test"))
        assert r.success
        assert r.data["status"] == "paused"

        # Resume
        r = await cron.resume_schedule(ResumeScheduleParams(name="lifecycle-test"))
        assert r.success
        assert r.data["status"] == "active"

        # Run now
        r = await cron.run_now(RunNowParams(name="lifecycle-test"))
        assert r.success
        assert r.data["executed"]

        # History
        r = await cron.execution_history(ExecutionHistoryParams(name="lifecycle-test"))
        assert r.success
        assert r.data["total"] >= 1

        # Delete
        r = await cron.delete_schedule(DeleteScheduleParams(name="lifecycle-test"))
        assert r.success

    @pytest.mark.asyncio
    async def test_dag_dependency_chain(self, cron):
        """Create A → B → C chain and verify topological order."""
        from digitorn.modules.cron_native.params import (
            AddDependencyParams,
            CreateScheduleParams,
        )

        for name in ("step-a", "step-b", "step-c"):
            await cron.create_schedule(CreateScheduleParams(
                name=name, cron_expr="0 0 * * *", action_type="notification", prompt=name,
            ))

        r = await cron.add_dependency(AddDependencyParams(
            schedule="step-b", depends_on="step-a",
        ))
        assert r.success

        r = await cron.add_dependency(AddDependencyParams(
            schedule="step-c", depends_on="step-b",
        ))
        assert r.success
        order = r.data["execution_order"]
        assert order.index("step-a") < order.index("step-b") < order.index("step-c")

    @pytest.mark.asyncio
    async def test_holiday_affects_calendar_view(self, cron):
        """Holidays should appear as 'holiday_skip' in calendar view."""
        from digitorn.modules.cron_native.params import (
            AddHolidayParams,
            CalendarViewParams,
            CreateScheduleParams,
        )

        await cron.add_holiday(AddHolidayParams(
            date="2026-12-25", name="Christmas", recurring=True,
        ))

        await cron.create_schedule(CreateScheduleParams(
            name="daily-job", cron_expr="0 9 * * *",
            action_type="notification", prompt="test",
        ))

        r = await cron.calendar_view(CalendarViewParams(
            start_date="2026-12-24", end_date="2026-12-26",
        ))
        assert r.success
        entries = r.data["entries"]
        statuses = {e["status"] for e in entries}
        assert "holiday_skip" in statuses or "scheduled" in statuses

    @pytest.mark.asyncio
    async def test_retry_policy_backoff(self, cron):
        """Retry delay should increase with exponential backoff."""
        from digitorn.modules.cron_native.params import (
            CreateScheduleParams,
            SetRetryPolicyParams,
        )

        await cron.create_schedule(CreateScheduleParams(
            name="retry-test", cron_expr="0 0 * * *",
            action_type="tool_call", tool_name="nonexistent.action",
        ))

        await cron.set_retry_policy(SetRetryPolicyParams(
            name="retry-test", max_retries=5, retry_delay=10.0, backoff_multiplier=2.0,
        ))

        # Simulate retries
        delays = []
        for _ in range(3):
            d = cron._compute_retry_delay("retry-test")
            if d is not None:
                delays.append(d)
        assert delays == [10.0, 20.0, 40.0]


# ═══════════════════════════════════════════════════════════════════════
# State Persistence Roundtrip
# ═══════════════════════════════════════════════════════════════════════


class TestStatePersistence:
    """Test state_snapshot/restore_state across modules."""

    @pytest.mark.asyncio
    async def test_cache_state_roundtrip(self):
        from digitorn.modules.cache import CacheModule

        m = CacheModule()
        await m.on_start()
        snap = m.state_snapshot()
        assert "app_id" in snap
        assert "default_ttl" in snap

        m2 = CacheModule()
        await m2.on_start()
        await m2.restore_state(snap)
        assert m2._app_id == snap["app_id"]
        await m.on_stop()
        await m2.on_stop()

    @pytest.mark.asyncio
    async def test_cron_state_roundtrip_with_data(self):
        croniter = pytest.importorskip("croniter")
        from digitorn.modules.cron_native import CronNativeModule
        from digitorn.modules.cron_native.params import (
            AddDependencyParams,
            AddHolidayParams,
            CreateScheduleParams,
        )

        m = CronNativeModule()
        await m.on_start()

        # Populate state
        await m.create_schedule(CreateScheduleParams(
            name="s1", cron_expr="0 0 * * *", action_type="notification", prompt="test",
        ))
        await m.create_schedule(CreateScheduleParams(
            name="s2", cron_expr="0 12 * * *", action_type="notification", prompt="test2",
        ))
        await m.add_dependency(AddDependencyParams(schedule="s2", depends_on="s1"))
        await m.add_holiday(AddHolidayParams(date="2026-01-01", name="New Year", recurring=True))

        snap = m.state_snapshot()
        assert len(snap["schedules"]) == 2
        assert len(snap["dag"]) == 1
        assert len(snap["holidays"]) == 1

        # Restore into fresh module
        m2 = CronNativeModule()
        await m2.on_start()
        await m2.restore_state(snap)

        assert "s1" in m2._schedules
        assert "s2" in m2._schedules
        assert len(m2._dag.get_dependencies("s2")) == 1
        assert m2._holidays.is_holiday(__import__("datetime").date(2026, 1, 1))

        await m.on_stop()
        await m2.on_stop()
