"""Module executor - parallel and concurrent action dispatch."""

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
    """Describes a single action to execute as part of a parallel batch."""

    module_id: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    context: ExecutionContext | None = None
    task_id: str = ""

@dataclass
class ActionTaskResult:
    """Result for a single task in a parallel batch."""

    task: ActionTask
    result: Any = None
    error: Exception | None = None
    success: bool = True
    duration_ms: float = 0.0

class ModuleExecutor:
    """Parallel action dispatcher."""

    def __init__(
        self,
        registry: "ModuleRegistry",
        *,
        default_timeout: float = 60.0,
    ) -> None:
        """"""
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
        """Execute all *tasks* concurrently on the event loop."""
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
        """Execute *tasks* one at a time in order."""
        results = []
        for task in tasks:
            results.append(await self._dispatch_one(task))
        return results

    async def run_one(self, task: ActionTask) -> ActionTaskResult:
        """Convenience wrapper for a single task."""
        return await self._dispatch_one(task)

    def _get_semaphore(self, module_id: str) -> asyncio.Semaphore | None:
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
                    "action_failed: %s.%s - %s",
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
    """Run multiple `module.execute()` calls concurrently."""
    async def _one(module: Any, action: str, params: dict, ctx: Any) -> Any:
        coro = module.execute(action, params, ctx)
        if timeout is not None:
            coro = asyncio.wait_for(coro, timeout=timeout)
        return await coro

    return list(await asyncio.gather(
        *[_one(m, a, p, c) for m, a, p, c in calls],
        return_exceptions=return_exceptions,
    ))
