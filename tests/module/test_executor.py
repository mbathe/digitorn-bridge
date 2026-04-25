"""Tests — Concurrency: parallel execution, execution_mode, ModuleExecutor.

Covers:
- asyncio.gather() runs @action handlers truly concurrently on the event loop
- 10+ parallel tasks complete without one blocking another
- execution_mode="threaded" offloads blocking sync handlers via asyncio.to_thread()
- execution_mode="threaded" does NOT freeze the event loop for concurrent tasks
- ModuleExecutor.run_parallel() returns ActionTaskResult per task
- ModuleExecutor result order matches task input order
- ModuleExecutor.return_exceptions=True collects failures without raising
- ModuleExecutor.return_exceptions=False re-raises on first failure
- ModuleExecutor enforces ModulePolicy.max_parallel_calls via semaphore
- ModuleExecutor.run_sequential() executes tasks one-at-a-time
- gather_actions() helper works with direct module references
- Concurrent execute() calls share no mutable state between invocations
- Timeout is honoured per task (asyncio.TimeoutError)
- Duration measurements are plausible
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, ModulePolicy, Platform
from digitorn.modules.decorators import action
from digitorn.modules.executor import ActionTask, ActionTaskResult, ModuleExecutor, gather_actions
from digitorn.modules.manifest import ModuleManifest
from digitorn.modules.registry import ModuleRegistry


# ── Test modules ──────────────────────────────────────────────────────────────


class SlowModule(BaseModule):
    """Each action sleeps for a configurable duration to verify parallelism."""

    MODULE_ID = "slow"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]

    @action(description="Sleep for N ms then return N.", permissions=["slow:read"])
    async def wait(self, params: dict, ctx: ExecutionContext | None = None) -> ActionResult:
        ms = params.get("ms", 100)
        await asyncio.sleep(ms / 1000)
        return ActionResult(success=True, data=ms)

    @action(description="Always fails.", permissions=["slow:read"])
    async def fail(self, params: dict, ctx: ExecutionContext | None = None) -> ActionResult:
        raise RuntimeError("deliberate error")

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self)


class BlockingModule(BaseModule):
    """Uses execution_mode='threaded' for a CPU-blocking sync handler."""

    MODULE_ID = "blocking"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]

    @action(
        description="Block the thread for N ms (sync, threaded mode).",
        permissions=["blocking:read"],
        execution_mode="threaded",
    )
    def heavy_compute(self, params: dict, ctx: ExecutionContext | None = None) -> ActionResult:
        # This is a plain sync def — blocks the **thread** not the event loop
        ms = params.get("ms", 50)
        time.sleep(ms / 1000)
        return ActionResult(success=True, data=f"done after {ms}ms")

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self)


class ThrottledModule(BaseModule):
    """max_parallel_calls=2 — at most 2 concurrent executions at any time."""

    MODULE_ID = "throttled"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    _active: int = 0        # shared class-level counter for the test
    _peak: int = 0

    @action(description="Track peak concurrency.", permissions=["throttled:read"])
    async def track(self, params: dict, ctx: ExecutionContext | None = None) -> ActionResult:
        ThrottledModule._active += 1
        ThrottledModule._peak = max(ThrottledModule._peak, ThrottledModule._active)
        await asyncio.sleep(0.05)  # 50 ms slot
        ThrottledModule._active -= 1
        return ActionResult(success=True, data=ThrottledModule._peak)

    def policy_rules(self) -> ModulePolicy:
        return ModulePolicy(max_parallel_calls=2)

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_registry(*module_classes) -> ModuleRegistry:
    reg = ModuleRegistry()
    for cls in module_classes:
        reg.register(cls)
    return reg


# ── Test group: basic async concurrency ───────────────────────────────────────


class TestAsyncConcurrency:

    @pytest.mark.asyncio
    async def test_10_parallel_tasks_faster_than_sequential(self):
        """10 tasks each sleeping 100ms should complete well under 10×100ms."""
        reg = _make_registry(SlowModule)
        exe = ModuleExecutor(reg)

        tasks = [ActionTask("slow", "wait", {"ms": 100}) for _ in range(10)]

        t0 = time.perf_counter()
        results = await exe.run_parallel(tasks)
        elapsed = time.perf_counter() - t0

        # All completed successfully
        assert all(r.success for r in results)
        # Parallel: should finish in ~100ms not 1000ms (generous 3× buffer)
        assert elapsed < 0.5, f"Expected ~0.1s, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_result_order_matches_task_order(self):
        """Results must be in the same order as the input tasks."""
        reg = _make_registry(SlowModule)
        exe = ModuleExecutor(reg)

        sleep_times = [50, 200, 10, 150, 30]
        tasks = [ActionTask("slow", "wait", {"ms": ms}) for ms in sleep_times]

        results = await exe.run_parallel(tasks)

        for i, r in enumerate(results):
            assert r.task.params["ms"] == sleep_times[i]
            assert r.result.data == sleep_times[i]

    @pytest.mark.asyncio
    async def test_asyncio_gather_direct_usage(self):
        """Direct asyncio.gather() on execute() calls works without ModuleExecutor."""
        reg = _make_registry(SlowModule)
        module = reg.get("slow")

        results = await asyncio.gather(
            module.execute("wait", {"ms": 50}),
            module.execute("wait", {"ms": 50}),
            module.execute("wait", {"ms": 50}),
        )
        assert len(results) == 3
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_gather_actions_helper(self):
        """gather_actions() helper runs multiple modules' actions concurrently."""
        reg = _make_registry(SlowModule)
        module = reg.get("slow")

        results = await gather_actions([
            (module, "wait", {"ms": 30}, None),
            (module, "wait", {"ms": 30}, None),
            (module, "wait", {"ms": 30}, None),
        ])
        assert len(results) == 3
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_calls_share_no_mutable_state(self):
        """Each execute() invocation is independent — results don't bleed across calls."""
        reg = _make_registry(SlowModule)
        exe = ModuleExecutor(reg)

        tasks = [ActionTask("slow", "wait", {"ms": i * 10}) for i in range(1, 6)]
        results = await exe.run_parallel(tasks)

        for i, r in enumerate(results):
            assert r.result.data == (i + 1) * 10  # each got its own params


# ── Test group: execution_mode="threaded" ─────────────────────────────────────


class TestThreadedExecutionMode:

    @pytest.mark.asyncio
    async def test_threaded_action_returns_correct_result(self):
        """A blocking sync handler marked execution_mode='threaded' still executes."""
        reg = _make_registry(BlockingModule)
        module = reg.get("blocking")

        result = await module.execute("heavy_compute", {"ms": 20})
        assert result.success is True
        assert "done after 20ms" in result.data

    @pytest.mark.asyncio
    async def test_threaded_actions_run_concurrently_without_blocking_loop(self):
        """Multiple threaded actions must not block each other on the event loop."""
        reg = _make_registry(BlockingModule)
        exe = ModuleExecutor(reg)

        tasks = [ActionTask("blocking", "heavy_compute", {"ms": 100}) for _ in range(4)]

        t0 = time.perf_counter()
        results = await exe.run_parallel(tasks)
        elapsed = time.perf_counter() - t0

        assert all(r.success for r in results)
        # 4 × 100ms threads in parallel: should complete in ~100ms, not 400ms
        assert elapsed < 0.35, f"Threads appear to be sequential: {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_threaded_and_async_tasks_interleave(self):
        """Async and threaded tasks can run side-by-side without deadlock."""
        reg = _make_registry(SlowModule, BlockingModule)
        exe = ModuleExecutor(reg)

        tasks = [
            ActionTask("slow",     "wait",          {"ms": 80}),
            ActionTask("blocking", "heavy_compute",  {"ms": 80}),
            ActionTask("slow",     "wait",          {"ms": 80}),
            ActionTask("blocking", "heavy_compute",  {"ms": 80}),
        ]

        t0 = time.perf_counter()
        results = await exe.run_parallel(tasks)
        elapsed = time.perf_counter() - t0

        assert all(r.success for r in results)
        assert elapsed < 0.35


# ── Test group: error handling in parallel ────────────────────────────────────


class TestParallelErrorHandling:

    @pytest.mark.asyncio
    async def test_return_exceptions_true_collects_failures(self):
        """With return_exceptions=True, failed tasks don't prevent others from completing."""
        reg = _make_registry(SlowModule)
        exe = ModuleExecutor(reg)

        tasks = [
            ActionTask("slow", "wait", {"ms": 20}),
            ActionTask("slow", "fail", {}),          # will fail
            ActionTask("slow", "wait", {"ms": 20}),
        ]

        results = await exe.run_parallel(tasks, return_exceptions=True)

        assert len(results) == 3
        assert results[0].success is True
        assert results[1].success is False
        assert isinstance(results[1].error, Exception)
        assert results[2].success is True

    @pytest.mark.asyncio
    async def test_return_exceptions_false_propagates_on_failure(self):
        """With return_exceptions=False, any failure raises immediately."""
        reg = _make_registry(SlowModule)
        exe = ModuleExecutor(reg)

        tasks = [ActionTask("slow", "fail", {})]

        with pytest.raises(Exception):
            await exe.run_parallel(tasks, return_exceptions=False)

    @pytest.mark.asyncio
    async def test_failed_task_has_error_attribute(self):
        reg = _make_registry(SlowModule)
        exe = ModuleExecutor(reg)

        results = await exe.run_parallel([ActionTask("slow", "fail", {})], return_exceptions=True)
        assert results[0].error is not None
        assert results[0].result is None

    @pytest.mark.asyncio
    async def test_timeout_cancels_slow_task(self):
        """Per-batch timeout terminates tasks that exceed the deadline."""
        reg = _make_registry(SlowModule)
        exe = ModuleExecutor(reg)

        tasks = [ActionTask("slow", "wait", {"ms": 500})]

        results = await exe.run_parallel(tasks, timeout=0.05, return_exceptions=True)
        assert results[0].success is False
        assert isinstance(results[0].error, asyncio.TimeoutError)


# ── Test group: ModulePolicy throttling ───────────────────────────────────────


class TestPolicyThrottling:

    @pytest.mark.asyncio
    async def test_max_parallel_calls_limits_concurrency(self):
        """Submitting 6 tasks to a module with max_parallel_calls=2 must never
        have more than 2 tasks running simultaneously.
        """
        # Reset class-level peak counter before this test
        ThrottledModule._active = 0
        ThrottledModule._peak = 0

        reg = _make_registry(ThrottledModule)
        exe = ModuleExecutor(reg)

        tasks = [ActionTask("throttled", "track", {}) for _ in range(6)]
        results = await exe.run_parallel(tasks)

        # All succeed
        assert all(r.success for r in results)
        # Peak concurrency must be ≤ 2
        assert ThrottledModule._peak <= 2, (
            f"Semaphore not enforced: peak was {ThrottledModule._peak}"
        )


# ── Test group: sequential execution ─────────────────────────────────────────


class TestSequentialExecution:

    @pytest.mark.asyncio
    async def test_sequential_runs_in_order(self):
        """run_sequential() must execute tasks one at a time in order."""
        order = []
        reg = _make_registry(SlowModule)

        # Patch the module to record call order
        module = reg.get("slow")
        original_execute = module.execute

        async def recording_execute(action, params, ctx=None):
            order.append(params.get("ms"))
            return await original_execute(action, params, ctx)

        module.execute = recording_execute  # type: ignore[method-assign]

        exe = ModuleExecutor(reg)
        sleep_times = [30, 10, 50, 20]
        tasks = [ActionTask("slow", "wait", {"ms": ms}) for ms in sleep_times]

        results = await exe.run_sequential(tasks)

        assert len(results) == 4
        assert all(r.success for r in results)
        assert order == sleep_times  # strictly in submission order

    @pytest.mark.asyncio
    async def test_sequential_slower_than_parallel(self):
        """Sanity: sequential task execution is slower than parallel for independent tasks."""
        reg = _make_registry(SlowModule)
        exe = ModuleExecutor(reg)

        tasks = [ActionTask("slow", "wait", {"ms": 50}) for _ in range(4)]

        t_par = time.perf_counter()
        await exe.run_parallel(tasks)
        elapsed_par = time.perf_counter() - t_par

        t_seq = time.perf_counter()
        await exe.run_sequential(tasks)
        elapsed_seq = time.perf_counter() - t_seq

        assert elapsed_seq > elapsed_par * 1.5, (
            f"Expected sequential ({elapsed_seq:.3f}s) to be > 1.5× parallel ({elapsed_par:.3f}s)"
        )


# ── Test group: ActionTaskResult ──────────────────────────────────────────────


class TestActionTaskResult:

    @pytest.mark.asyncio
    async def test_result_has_duration(self):
        reg = _make_registry(SlowModule)
        exe = ModuleExecutor(reg)

        results = await exe.run_parallel([ActionTask("slow", "wait", {"ms": 50})])
        r = results[0]
        assert r.duration_ms >= 40  # at least 40ms
        assert r.duration_ms < 1000  # sanity upper bound

    @pytest.mark.asyncio
    async def test_result_task_reference_is_original(self):
        reg = _make_registry(SlowModule)
        exe = ModuleExecutor(reg)

        task = ActionTask("slow", "wait", {"ms": 10}, task_id="my-task-42")
        results = await exe.run_parallel([task])

        assert results[0].task is task
        assert results[0].task.task_id == "my-task-42"

    @pytest.mark.asyncio
    async def test_run_one_convenience(self):
        reg = _make_registry(SlowModule)
        exe = ModuleExecutor(reg)

        result = await exe.run_one(ActionTask("slow", "wait", {"ms": 10}))
        assert isinstance(result, ActionTaskResult)
        assert result.success is True
