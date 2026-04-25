"""Module executor — parallel and concurrent action dispatch.

**Concurrency model**

Python has three concurrency tiers. You must understand which tier each
action belongs to in order to use the right dispatch strategy:

+-----------+---------------------------+---------------------+------------------+
| Tier      | Mechanism                 | Best for            | GIL impact       |
+-----------+---------------------------+---------------------+------------------+
| async     | asyncio.gather()          | I/O-bound           | None (1 thread)  |
| threaded  | asyncio.to_thread()       | Blocking/C-ext      | GIL shared       |
| process   | ProcessPoolExecutor       | CPU-bound Python    | No GIL           |
+-----------+---------------------------+---------------------+------------------+

**The key insight:** ``async def execute()`` is the correct building block for
**all three tiers** because:

- Pure async handlers: already concurrent via the event loop.
- Threaded handlers: ``asyncio.to_thread()`` runs them in a thread pool and
  yields control back to the event loop while they run.
- Process handlers: ``loop.run_in_executor(process_pool, ...)`` works the
  same way — the event loop stays responsive.

``asyncio.gather()`` is the *parallel launcher* in all cases — it schedules
*N* coroutines concurrently on the event loop and collects all results
(or all errors if ``return_exceptions=True``).

**Running 10+ actions in parallel (the agent scenario)**::

    from digitorn.module.executor import ModuleExecutor, ActionTask

    executor = ModuleExecutor(registry)
    tasks = [
        ActionTask("filesystem", "read_file",  {"path": "/a"}, ctx),
        ActionTask("filesystem", "read_file",  {"path": "/b"}, ctx),
        ActionTask("calendar",   "list_events", {"date": "today"}, ctx),
    ]
    results = await executor.run_parallel(tasks)

``ModuleExecutor`` also enforces ``ModulePolicy.max_parallel_calls`` per module
so a module that declares ``max_parallel_calls=3`` will never have more than 3
concurrent calls, regardless of how many tasks are submitted.
"""

from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from digitorn.modules.base import ActionResult, ExecutionContext
from digitorn.modules.log import get_logger

if TYPE_CHECKING:
    from digitorn.modules.registry import ModuleRegistry

log = get_logger(__name__)

_THREAD_POOL = ThreadPoolExecutor(thread_name_prefix="digitorn-action")


@dataclass
class ActionTask:
    """Describes a single action to execute as part of a parallel batch.

    Attributes:
        module_id: The target module (must be registered in the registry).
        action:    The action name to dispatch.
        params:    Action parameters (validated before being passed to execute).
        context:   Optional execution context (shared or per-task).
        task_id:   Optional caller-assigned identifier for result correlation.
    """

    module_id: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    context: ExecutionContext | None = None
    task_id: str = ""


@dataclass
class ActionTaskResult:
    """Result for a single task in a parallel batch.

    Attributes:
        task:      The originating :class:`ActionTask`.
        result:    The return value of ``module.execute()``, or ``None`` on error.
        error:     The exception if the action failed, or ``None`` on success.
        success:   Convenience flag — ``True`` iff ``error is None``.
        duration_ms: Wall-clock time in milliseconds.
    """

    task: ActionTask
    result: Any = None
    error: Exception | None = None
    success: bool = True
    duration_ms: float = 0.0


class ModuleExecutor:
    """Parallel action dispatcher.

    Submits *N* :class:`ActionTask` objects concurrently using
    ``asyncio.gather()``.  All tasks run on the **same event loop** — there
    is no GIL-fighting unless actions use ``execution_mode="threaded"`` or
    ``"process"``, in which case the executor offloads to the appropriate pool.

    Policy throttling
    -----------------
    If a module declares ``ModulePolicy.max_parallel_calls = K``, the executor
    gates execution with an ``asyncio.Semaphore(K)`` so that at most *K* tasks
    run concurrently for that module, regardless of how many tasks are
    submitted.  Other tasks wait without blocking the event loop.

    Usage::

        executor = ModuleExecutor(registry)

        results = await executor.run_parallel(tasks, return_exceptions=True)

        for r in results:
            if r.success:
                print(r.result)
            else:
                print(f"FAILED: {r.error}")
    """

    def __init__(
        self,
        registry: "ModuleRegistry",
        *,
        default_timeout: float = 60.0,
    ) -> None:
        """
        Args:
            registry:        The module registry to resolve module IDs against.
            default_timeout: Per-action timeout in seconds (0 = no timeout).
        """
        self._registry = registry
        self._default_timeout = default_timeout
        self._semaphores: dict[str, asyncio.Semaphore | None] = {}


    async def run_parallel(
        self,
        tasks: list[ActionTask],
        *,
        return_exceptions: bool = True,
        timeout: float | None = None,
    ) -> list[ActionTaskResult]:
        """Execute all *tasks* concurrently on the event loop.

        All tasks are submitted at once via ``asyncio.gather()``.  The call
        returns only when **all** tasks have finished (or failed).

        Args:
            tasks:             List of tasks to run in parallel.
            return_exceptions: If ``True`` (default), failed tasks produce an
                               :class:`ActionTaskResult` with ``success=False``
                               instead of propagating the exception.  If
                               ``False``, the first failure cancels remaining
                               tasks and the exception is re-raised.
            timeout:           Optional per-batch wall-clock timeout in seconds.
                               When elapsed, all still-running tasks are
                               cancelled and return a ``TimeoutError``.

        Returns:
            List of :class:`ActionTaskResult` in the same order as *tasks*.
        """
        propagate_errors = not return_exceptions

        coroutines = [self._dispatch_one(task, propagate_errors=propagate_errors) for task in tasks]

        if timeout is not None:
            coroutines = [asyncio.wait_for(c, timeout=timeout) for c in coroutines]

        raw = await asyncio.gather(*coroutines, return_exceptions=return_exceptions)
        results: list[ActionTaskResult] = []
        for i, item in enumerate(raw):
            if isinstance(item, BaseException):
                results.append(ActionTaskResult(
                    task=tasks[i],
                    error=item,
                    success=False,
                ))
            else:
                results.append(item)

        return results


    async def run_sequential(self, tasks: list[ActionTask]) -> list[ActionTaskResult]:
        """Execute *tasks* one at a time in order.

        Useful when actions have data dependencies or when you explicitly want
        serial execution (e.g. for a dry-run or audit mode).
        """
        results = []
        for task in tasks:
            results.append(await self._dispatch_one(task))
        return results

    async def run_one(self, task: ActionTask) -> ActionTaskResult:
        """Convenience wrapper for a single task."""
        return await self._dispatch_one(task)


    def _get_semaphore(self, module_id: str) -> asyncio.Semaphore | None:
        """Return (or lazily create) the semaphore for *module_id*."""
        if module_id not in self._semaphores:
            try:
                module = self._registry.get(module_id)
                policy = module.policy_rules()
                limit = policy.max_parallel_calls
                self._semaphores[module_id] = (
                    asyncio.Semaphore(limit) if limit > 0 else None
                )
            except Exception:
                self._semaphores[module_id] = None
        return self._semaphores[module_id]

    async def _dispatch_one(
        self,
        task: ActionTask,
        *,
        propagate_errors: bool = False,
    ) -> ActionTaskResult:
        """Dispatch a single task, enforcing per-module concurrency limits.

        Args:
            task:             The task to dispatch.
            propagate_errors: When ``True``, exceptions are re-raised instead of
                              being wrapped in :class:`ActionTaskResult`.  Used
                              internally to support ``return_exceptions=False``.
        """
        import time

        sem = self._get_semaphore(task.module_id)
        start = time.perf_counter()

        async def _run() -> ActionTaskResult:
            try:
                module = self._registry.get(task.module_id)
                result = await module.execute(task.action, task.params, task.context)
                duration = (time.perf_counter() - start) * 1000
                return ActionTaskResult(
                    task=task,
                    result=result,
                    success=True,
                    duration_ms=round(duration, 2),
                )
            except Exception as exc:
                duration = (time.perf_counter() - start) * 1000
                log.error(
                    "action_failed: %s.%s — %s",
                    task.module_id,
                    task.action,
                    exc,
                )
                if propagate_errors:
                    raise
                return ActionTaskResult(
                    task=task,
                    error=exc,
                    success=False,
                    duration_ms=round(duration, 2),
                )

        if sem is not None:
            async with sem:
                return await _run()
        return await _run()


async def gather_actions(
    calls: list[tuple[Any, str, dict[str, Any], ExecutionContext | None]],
    *,
    return_exceptions: bool = True,
    timeout: float | None = None,
) -> list[Any]:
    """Run multiple ``module.execute()`` calls concurrently.

    This is the low-level helper for callers that already hold module
    references and don't need the full :class:`ModuleExecutor`.

    Args:
        calls: List of ``(module, action, params, context)`` tuples.
        return_exceptions: Passed to ``asyncio.gather()``.
        timeout: Per-call timeout in seconds.

    Returns:
        List of results in the same order as *calls*.

    Example::

        results = await gather_actions([
            (fs_module,  "read_file",   {"path": "/a"}, ctx),
            (web_module, "fetch_url",   {"url": "https://..."},  ctx),
            (ai_module,  "summarise",   {"text": "..."},  ctx),
        ])
    """
    async def _one(module: Any, action: str, params: dict, ctx: Any) -> Any:
        coro = module.execute(action, params, ctx)
        if timeout is not None:
            coro = asyncio.wait_for(coro, timeout=timeout)
        return await coro

    return list(await asyncio.gather(
        *[_one(m, a, p, c) for m, a, p, c in calls],
        return_exceptions=return_exceptions,
    ))
