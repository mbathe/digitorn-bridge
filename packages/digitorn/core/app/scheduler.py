"""SchedulerService - fires due jobs via per-job asyncio tasks.

One instance per daemon process. Persistence lives in JobStore (KV).
The scheduler keeps an in-memory map of asyncio tasks, one per active
job, that sleep until the job's ``next_run_at`` and then fire it.

Cron parsing is delegated entirely to ``croniter`` (already a hard
dependency, no fallback parser).

Architecture
------------
- At ``start()``: load all active jobs from JobStore and create one
  asyncio task per job.
- ``register_job(job)``: cancel any existing task for this job, then
  create a fresh task that sleeps until ``next_run_at``.
- ``_wait_and_fire(job)``: ``asyncio.sleep`` until the target epoch,
  then call ``_fire_job``. ``_fire_job`` recomputes ``next_run_at``,
  persists, delivers via channel, and re-schedules the next run by
  calling ``register_job(job)`` again.
- ``unregister_job``: cancel the task and remove from the map.

This replaces the previous heap+tick-loop design. The new model is
~50 lines shorter and behaves identically from the outside.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from croniter import croniter

from digitorn.core.app.job_store import ScheduledJob

if TYPE_CHECKING:
    from digitorn.core.app.job_store import JobStore
    from digitorn.core.app.output_channels import ChannelRegistry

logger = logging.getLogger(__name__)


class SchedulerService:
    """Global scheduler that fires due jobs via per-job asyncio tasks.

    Args:
        job_store: Persistence layer for jobs.
        channel_registry: Output channel registry for delivering results.
    """

    def __init__(
        self,
        job_store: JobStore,
        channel_registry: ChannelRegistry,
    ) -> None:
        self._job_store = job_store
        self._channels = channel_registry
        self._running = False
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._app_executors: dict[str, Any] = {}
        self._wake_handlers: dict[str, Any] = {}

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the scheduler and spawn one task per active job."""
        if self._running:
            return
        self._running = True
        jobs = self._job_store.list_all_active_jobs()
        for job in jobs:
            self._spawn_task(job)
        logger.info("scheduler_started jobs_loaded=%d", len(jobs))

    async def stop(self) -> None:
        """Cancel every per-job task and shut down."""
        self._running = False
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        # Drain so cancellations propagate cleanly
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        logger.info("scheduler_stopped")

    # ── Public API (unchanged) ──────────────────────────────────────

    def register_job(self, job: ScheduledJob) -> None:
        """Add or update a job. Cancels any existing task for this id
        and spawns a fresh one that fires at ``job.next_run_at``."""
        self._cancel_task(job.app_id, job.job_id)
        self._spawn_task(job)

    def unregister_job(self, app_id: str, job_id: str) -> None:
        """Cancel and forget a job."""
        self._cancel_task(app_id, job_id)

    def register_app_executor(self, app_id: str, executor: Any) -> None:
        """Register a callback for executing tools within an app.

        ``executor`` must expose an async ``execute(action, params)``
        method - typically the ContextBuilderModule.
        """
        self._app_executors[app_id] = executor

    def register_wake_handler(self, app_id: str, handler: Any) -> None:
        """Register an async ``handler(session_id, message)`` that wakes
        a session and runs a new agent turn with ``message`` as a system
        reminder. Used for session-bound reminder jobs."""
        self._wake_handlers[app_id] = handler

    def unregister_wake_handler(self, app_id: str) -> None:
        self._wake_handlers.pop(app_id, None)

    def unregister_app_executor(self, app_id: str) -> None:
        """Drop the executor (called on undeploy)."""
        self._app_executors.pop(app_id, None)

    # ── Per-job task management ─────────────────────────────────────

    def _composite(self, app_id: str, job_id: str) -> str:
        return f"{app_id}:{job_id}"

    def _spawn_task(self, job: ScheduledJob) -> None:
        if not self._running:
            return
        if job.status != "active" or not job.next_run_at:
            return
        composite = self._composite(job.app_id, job.job_id)
        try:
            target_epoch = datetime.fromisoformat(job.next_run_at).timestamp()
        except (ValueError, TypeError):
            logger.warning(
                "scheduler_bad_next_run job=%s next_run=%s",
                job.job_id, job.next_run_at,
            )
            return
        task = asyncio.create_task(self._wait_and_fire(job, target_epoch))
        self._tasks[composite] = task

    def _cancel_task(self, app_id: str, job_id: str) -> None:
        composite = self._composite(app_id, job_id)
        task = self._tasks.pop(composite, None)
        if task and not task.done():
            task.cancel()

    async def _wait_and_fire(self, job: ScheduledJob, target_epoch: float) -> None:
        """Sleep until target_epoch, then fire the job once."""
        try:
            delay = max(0.0, target_epoch - time.time())
            if delay:
                await asyncio.sleep(delay)
            if not self._running:
                return
            # Re-fetch in case the job was paused or modified while we slept
            current = self._job_store.get_job(job.app_id, job.job_id)
            if current is None or current.status != "active":
                return
            await self._fire_job(current)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "scheduler_wait_and_fire_crashed job=%s app=%s",
                job.job_id, job.app_id,
            )

    # ── Action execution ────────────────────────────────────────────

    async def _fire_job(self, job: ScheduledJob) -> None:
        """Execute a job's action, persist state, deliver result.

        Persistence is atomic: all state updates (run_count, next_run_at,
        status, last_*) are computed first, then written in a single KV
        write BEFORE the result is delivered. If the daemon crashes
        between fire and persist, the old next_run_at remains and the
        job re-fires on restart (at-least-once semantics).
        """
        now = datetime.now(timezone.utc)
        job.run_count += 1
        job.last_run_at = now.isoformat()

        result: Any = None
        error: str | None = None
        woke_session = False

        try:
            if job.action_type == "tool_call" and job.tool_name:
                result, error = await self._execute_tool(
                    job.app_id, job.tool_name, job.tool_params,
                )
            elif job.action_type == "llm_prompt" and job.session_id:
                handler = self._wake_handlers.get(job.app_id)
                if handler is not None:
                    try:
                        await handler(job.session_id, job.prompt or "")
                        woke_session = True
                        result = {"woke_session": job.session_id}
                    except Exception as exc:
                        error = f"Wake handler failed: {exc}"
                else:
                    error = f"No wake handler registered for app {job.app_id}"
            elif job.action_type in ("llm_prompt", "notification"):
                result = {"prompt": job.prompt, "memory_context": job.memory_context}
            else:
                error = f"Unknown action_type: {job.action_type}"
        except Exception as exc:
            error = str(exc)
            logger.exception(
                "scheduler_job_fire_error job=%s app=%s",
                job.job_id, job.app_id,
            )

        job.last_result = result
        job.last_error = error

        if _should_complete(job):
            job.status = "completed"
            job.next_run_at = None
        else:
            job.next_run_at = _compute_next_run(job, now)

        self._job_store.persist_job_atomic(job)

        if not woke_session:
            payload: dict[str, Any] = {
                "type": "scheduled_job",
                "job_id": job.job_id,
                "label": job.label,
                "action_type": job.action_type,
                "schedule_type": job.schedule_type,
                "run_count": job.run_count,
            }
            if error:
                payload["error"] = error
            else:
                payload["result"] = result
            if job.prompt:
                payload["prompt"] = job.prompt
            if job.memory_context:
                payload["memory_context"] = job.memory_context

            await self._channels.deliver(
                job.output_channel, job.app_id, payload, job.output_config,
                session_id=job.session_id,
            )

        if job.status == "active" and job.next_run_at:
            self._spawn_task(job)

        logger.info(
            "scheduler_job_fired job=%s app=%s type=%s run=%d woke=%s%s",
            job.job_id, job.app_id, job.action_type, job.run_count,
            woke_session, f" error={error}" if error else "",
        )

    async def _execute_tool(
        self, app_id: str, tool_name: str, params: dict[str, Any],
    ) -> tuple[Any, str | None]:
        """Run a tool call via the app's context_builder executor."""
        executor = self._app_executors.get(app_id)
        if executor is None:
            return None, f"App '{app_id}' not deployed (executor not registered)"
        try:
            from digitorn.modules.base import ActionResult

            result = await executor.execute(
                "execute_tool",
                {"name": tool_name, "params": params},
            )
            if isinstance(result, ActionResult):
                if result.success:
                    return result.data, None
                return result.data, result.error
            return result, None
        except Exception as exc:
            return None, str(exc)


# ── Helpers (module-level, no state) ─────────────────────────────────


def _should_complete(job: ScheduledJob) -> bool:
    """True if a job has reached its terminal state."""
    if job.schedule_type == "once":
        return True
    if job.max_runs > 0 and job.run_count >= job.max_runs:
        return True
    return False


def _compute_next_run(job: ScheduledJob, now: datetime) -> str | None:
    """Compute the next run time for a recurring job via croniter."""
    if job.schedule_type == "interval" and job.interval_seconds:
        from datetime import timedelta
        nxt = now + timedelta(seconds=job.interval_seconds)
        return nxt.isoformat()
    if job.schedule_type == "cron" and job.cron_expr:
        cron = croniter(job.cron_expr, now)
        nxt = cron.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        return nxt.isoformat()
    return None
