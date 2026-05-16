"""Warm Worker Pool - pre-bootstrapped workers for per-session sandboxing.

Workers are pre-spawned and bootstrapped (expensive: ~2-5s), then sit
warm in a pool.  When a session starts, a warm worker receives the
workspace via IPC and applies Landlock in ~0.1ms.

Worker state machine:
    [SPAWNING] → bootstrap → [WARM] → sandbox(ws) → [SANDBOXED] → session end → [TAINTED] → kill → recycle
                                ▲                         │
                                │                         │ same workspace
                                │                         ▼
                                │                    reuse for new session
                              respawn into pool

Workspace affinity: if an existing SANDBOXED worker has the same
workspace, it can be reused (multiple sessions share the sandbox).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .worker import AppSandboxWorker, WorkerState

logger = logging.getLogger(__name__)


class WorkerPool:
    """Manages pre-bootstrapped workers for per-session sandboxing."""

    def __init__(
        self,
        compiled: Any,
        app_id: str,
        *,
        pool_size: int | None = None,
        pool_max: int = 8,
        namespaces: set[str] | None = None,
        hardening: dict[str, Any] | None = None,
        audit: bool = False,
        workspace_snapshot: bool = False,
    ) -> None:
        # Load sandbox config, fall back to hardcoded defaults
        try:
            from digitorn.core.config import get_settings
            _cfg = get_settings().sandbox
            _default_pool_size = _cfg.pool_size
            _default_idle_timeout = _cfg.idle_timeout
        except Exception:
            _default_pool_size = 2
            _default_idle_timeout = 60.0

        if pool_size is None:
            pool_size = _default_pool_size

        self._compiled = compiled
        self._app_id = app_id
        # ``pool_size=0`` is the explicit "no prewarm" opt-out for
        # machines where parallel cold imports saturate the disk
        # (Windows + AV, slow SSDs, ...). Workers spawn lazily on
        # first session use. clamp(0, ...) preserves the disable
        # path while keeping the legacy positive defaults.
        self._pool_size = max(0, pool_size)
        self._pool_max = max(self._pool_size, pool_max)
        self._namespaces = namespaces or set()
        self._hardening = hardening or {}
        self._audit = audit
        self._workspace_snapshot = workspace_snapshot

        self._warm: list[AppSandboxWorker] = []
        self._active: dict[str, AppSandboxWorker] = {}  # session_id → worker
        self._tainted: list[AppSandboxWorker] = []
        self._last_active: dict[str, float] = {}
        self._snapshots: dict[str, Any] = {}
        self._pending_workspaces: dict[str, asyncio.Future[AppSandboxWorker]] = {}  # workspace → future

        self._lock = asyncio.Lock()
        self._replenish_task: asyncio.Task[None] | None = None
        self._idle_reaper_task: asyncio.Task[None] | None = None
        # Track all fire-and-forget tasks (e.g. _recycle_tainted) so shutdown
        # can wait for them and prevent orphaned worker processes.
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._running = False
        self._idle_timeout = _default_idle_timeout

    def _spawn_background(self, coro: Any) -> asyncio.Task[None]:
        """Create and track a background task. Use instead of bare create_task."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    @property
    def stats(self) -> dict[str, int]:
        return {
            "warm": len(self._warm),
            "active": len(self._active),
            "tainted": len(self._tainted),
            "total": len(self._warm) + len(self._active) + len(self._tainted),
        }

    async def start(self) -> None:
        """Pre-warm the pool with pool_size workers."""
        self._running = True

        # ``pool_size == 0`` is the explicit opt-out: no prewarming,
        # workers will spawn lazily on first ``acquire``. Skip the
        # asyncio.gather entirely so we don't even create the empty
        # task list and short-circuit the log to a single line.
        if self._pool_size > 0:
            # Spawn initial workers in parallel
            tasks = [self._spawn_warm_worker() for _ in range(self._pool_size)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for r in results:
                if isinstance(r, Exception):
                    logger.warning("pool_warm_error app=%s: %s", self._app_id, r)

            # Start background replenisher only when prewarming is enabled.
            self._replenish_task = asyncio.create_task(self._replenish_loop())

        # Idle reaper always runs - it cleans up tainted/idle workers
        # that materialised via the lazy acquire path too.
        self._idle_reaper_task = asyncio.create_task(self._idle_reaper_loop())

        logger.info(
            "pool_started app=%s warm=%d target=%d max=%d",
            self._app_id, len(self._warm), self._pool_size, self._pool_max,
        )

    async def acquire(
        self,
        workspace: str,
        session_id: str,
    ) -> AppSandboxWorker:
        """Get a sandboxed worker for the given session.

        1. Check workspace affinity - reuse existing sandboxed worker
        2. Check pending sandboxing - wait for worker being set up for same workspace
        3. Take a warm worker from pool, apply sandbox
        4. If pool empty, spawn a new worker on demand

        Returns the ready worker (state: SANDBOXED).

        Thread-safe: uses _lock for all state mutations + _pending_workspaces
        to prevent duplicate sandboxing for the same workspace under concurrency.
        """
        async with self._lock:
            # Workspace affinity: reuse active worker with same workspace
            for sid, worker in self._active.items():
                if worker.workspace == workspace and worker.state == WorkerState.SANDBOXED:
                    self._active[session_id] = worker
                    self._last_active[session_id] = asyncio.get_event_loop().time()
                    return worker

            # Check if another acquire is already sandboxing for this workspace
            if workspace in self._pending_workspaces:
                fut = self._pending_workspaces[workspace]
            else:
                fut = None

        # Wait for pending sandbox to finish, then reuse via affinity
        if fut is not None:
            worker = await asyncio.shield(fut)
            async with self._lock:
                self._active[session_id] = worker
                self._last_active[session_id] = asyncio.get_event_loop().time()
            return worker

        # No existing worker for this workspace - create one
        pending_fut: asyncio.Future[AppSandboxWorker] = asyncio.get_event_loop().create_future()
        async with self._lock:
            # Double-check after re-acquiring lock (another task may have finished)
            for sid, worker in self._active.items():
                if worker.workspace == workspace and worker.state == WorkerState.SANDBOXED:
                    self._active[session_id] = worker
                    self._last_active[session_id] = asyncio.get_event_loop().time()
                    return worker

            self._pending_workspaces[workspace] = pending_fut
            worker = self._warm.pop() if self._warm else None

        try:
            if worker is None:
                total = len(self._warm) + len(self._active) + len(self._tainted)
                if total >= self._pool_max:
                    raise RuntimeError(
                        f"Worker pool exhausted ({total}/{self._pool_max}) "
                        f"for app {self._app_id}"
                    )
                worker = await self._spawn_warm_worker()

            # Create workspace snapshot if enabled
            effective_workspace = workspace
            if self._workspace_snapshot and workspace:
                try:
                    from .overlay import WorkspaceSnapshot
                    snapshot = WorkspaceSnapshot(workspace, session_id)
                    effective_workspace = await snapshot.create()
                    self._snapshots[session_id] = snapshot
                except Exception as exc:
                    logger.warning("pool_snapshot_failed session=%s: %s", session_id, exc)

            # Apply sandbox
            await worker.apply_sandbox(
                workspace=effective_workspace,
                session_id=session_id,
                namespaces=self._namespaces,
                hardening=self._hardening,
                audit=self._audit,
            )

            async with self._lock:
                self._active[session_id] = worker
                self._last_active[session_id] = asyncio.get_event_loop().time()
                self._pending_workspaces.pop(workspace, None)

            # Notify waiters
            if not pending_fut.done():
                pending_fut.set_result(worker)

            return worker

        except Exception as exc:
            async with self._lock:
                self._pending_workspaces.pop(workspace, None)
            if not pending_fut.done():
                pending_fut.set_exception(exc)
            raise

    async def release(self, session_id: str) -> None:
        """Release a worker when a session ends.

        If no other sessions share this worker, mark it tainted
        and schedule recycling. Cleans up workspace snapshots.
        """
        # Clean up workspace snapshot
        snapshot = self._snapshots.pop(session_id, None)
        if snapshot is not None:
            try:
                await snapshot.discard()
            except Exception as exc:
                # Log at warning since this can leak disk space if it keeps failing
                logger.warning(
                    "pool_snapshot_cleanup_error session=%s: %s - disk leak risk",
                    session_id, exc, exc_info=True,
                )

        async with self._lock:
            worker = self._active.pop(session_id, None)
            self._last_active.pop(session_id, None)
            if worker is None:
                return

            # Check if another session still uses this worker
            still_used = any(
                w is worker for sid, w in self._active.items()
            )

            if not still_used:
                worker.mark_tainted()
                self._tainted.append(worker)

        # Recycle tainted workers in background - tracked so shutdown can wait
        self._spawn_background(self._recycle_tainted())

    async def shutdown(self) -> None:
        """Stop all workers and clean up.

        Properly awaits all background tasks (replenish, idle reaper, recycle)
        so we don't leave orphaned worker processes running.
        """
        self._running = False

        # Cancel and await long-running loops
        for task in (self._replenish_task, self._idle_reaper_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.debug("background_task_shutdown_error: %s", exc)

        # Wait for any in-flight fire-and-forget background tasks (recycle, etc.)
        if self._background_tasks:
            pending = list(self._background_tasks)
            for t in pending:
                t.cancel()
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception as exc:
                logger.debug("background_tasks_gather_error: %s", exc)
            self._background_tasks.clear()

        # Stop all workers
        all_workers: list[AppSandboxWorker] = []
        async with self._lock:
            all_workers.extend(self._warm)
            all_workers.extend(self._active.values())
            all_workers.extend(self._tainted)
            self._warm.clear()
            self._active.clear()
            self._tainted.clear()

        # Deduplicate (shared workers)
        seen: set[int] = set()
        unique: list[AppSandboxWorker] = []
        for w in all_workers:
            if id(w) not in seen:
                seen.add(id(w))
                unique.append(w)

        stops = [w.stop() for w in unique]
        await asyncio.gather(*stops, return_exceptions=True)

        logger.info("pool_shutdown app=%s workers=%d", self._app_id, len(unique))

    # ── Internal ──────────────────────────────────────────────────

    async def _spawn_warm_worker(self) -> AppSandboxWorker:
        """Spawn and bootstrap a new warm worker."""
        worker = AppSandboxWorker(
            self._compiled, self._app_id, warm_pool=True,
        )
        await worker.start()

        if worker.state == WorkerState.WARM:
            async with self._lock:
                self._warm.append(worker)
            return worker

        raise RuntimeError(f"Worker failed to reach WARM state: {worker.state}")

    async def _recycle_tainted(self) -> None:
        """Kill tainted workers and replenish the warm pool."""
        async with self._lock:
            to_kill = list(self._tainted)
            self._tainted.clear()

        for worker in to_kill:
            try:
                await worker.stop()
            except Exception as exc:
                logger.debug("pool_recycle_error: %s", exc)

    async def _replenish_loop(self) -> None:
        """Background task: keep the warm pool at target size."""
        try:
            while self._running:
                await asyncio.sleep(1.0)

                async with self._lock:
                    deficit = self._pool_size - len(self._warm)
                    total = len(self._warm) + len(self._active) + len(self._tainted)
                    headroom = self._pool_max - total

                spawn_count = min(deficit, headroom)
                if spawn_count <= 0:
                    continue

                tasks = [self._spawn_warm_worker() for _ in range(spawn_count)]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        logger.debug("pool_replenish_error: %s", r)

        except asyncio.CancelledError:
            pass

    async def _idle_reaper_loop(self) -> None:
        """Release workers for sessions that have been idle too long."""
        try:
            while self._running:
                await asyncio.sleep(30.0)
                now = asyncio.get_event_loop().time()
                to_release: list[str] = []

                async with self._lock:
                    for sid, ts in self._last_active.items():
                        if now - ts > self._idle_timeout:
                            to_release.append(sid)

                for sid in to_release:
                    await self.release(sid)
                    logger.info("pool_idle_release app=%s session=%s", self._app_id, sid)
        except asyncio.CancelledError:
            pass
