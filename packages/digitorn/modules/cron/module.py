"""CronModule: background-task host for workers.

Hosts the activation sweep loop + (future) other periodic
maintenance jobs. Uses a file-based leader lock so even if multiple
workers list ``cron`` in their config, only one actually runs the
sweep -- the others stay idle until the leader releases / dies.

When the cron module is **not** loaded (the default, no worker hosts
it), the daemon's existing ``_activation_sweeper`` runs in the
lifespan as today -- guarded by a registry check in ``server.py`` so
we don't double-execute the sweep when cron IS workered.

Design notes
============

* No ``@action`` decorators -- this module is invisible to the LLM.
  All work happens inside ``on_start`` / ``on_stop``.
* The leader lock lives in ``~/.digitorn/.cron-leader.lock`` next to
  the workers shared secret. File-based, no Postgres dependency
  (works in local mode where the user has no DB).
* The sweep iteration imports ``ActivationStore`` lazily so loading
  the module never pulls in the DB stack if it isn't already
  initialised. When DB is unavailable (e.g. SQLite-only local mode
  without the activations table), the sweep is a clean no-op.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from digitorn.modules.base import BaseModule, Platform
from digitorn.modules.manifest import ModuleManifest

logger = logging.getLogger(__name__)


# Tunables. Conservative defaults matched to the daemon's existing
# ``_activation_sweeper`` so behaviour is invisible to operators on
# the data plane.
_SWEEP_INTERVAL_S = 60.0
_SWEEP_OLDER_THAN_S = 600
_LEADER_LOCK_FILENAME = ".cron-leader.lock"


class CronModule(BaseModule):
    """Singleton-by-lease background scheduler."""

    MODULE_ID = "cron"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    MODULE_TYPE = "system"
    # Per-app instance via registry.create -- but only one worker
    # process at a time hosts ``cron``, so MODULE_SINGLETON would be
    # functionally equivalent. Keep False so per-app config can
    # override sweep intervals in the future without affecting other
    # apps loaded by the same worker.
    MODULE_SINGLETON = False

    def __init__(self) -> None:
        super().__init__()
        self._leader: Any | None = None  # FileLeader -- typed Any to keep
                                          # the workers import out of the
                                          # module's hot import path.
        self._sweep_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._stopping = False
        self._sweep_interval_s = _SWEEP_INTERVAL_S
        self._sweep_older_than_s = _SWEEP_OLDER_THAN_S

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=self.MODULE_ID,
            version=self.VERSION,
            description=(
                "Worker-hosted background scheduler. Runs activation "
                "sweeps under a file-based leader lock."
            ),
            module_type="system",
            tags=["system", "background", "cron", "scheduler"],
        )

    async def on_start(self) -> None:
        """Acquire the leader lock + start background tasks.

        If the lock is held by another live cron instance, this
        method logs and returns -- the module stays loaded but its
        background tasks never start. ``on_stop`` is safe to call
        in that state (no-ops).
        """
        # ── Daemon-vs-worker disambiguation ─────────────────────
        # This ``on_start`` is reached from THREE paths:
        #   1. Daemon ``ModuleLifecycleManager.start_all()`` -- every
        #      registered module's on_start is invoked unconditionally
        #      at daemon boot.
        #   2. Per-app daemon bootstrap (when an app declares cron).
        #   3. Worker process boot (when ``cron`` is in the worker's
        #      hosted_modules list).
        #
        # When workers are enabled AND host ``cron``, ONLY path 3
        # should acquire the leader -- otherwise the daemon races the
        # worker and the worker stays idle forever. The fork is via
        # the ``DIGITORN_INSIDE_WORKER`` env var the lifecycle sets
        # at spawn time.
        try:
            inside_worker = os.environ.get(
                "DIGITORN_INSIDE_WORKER", "",
            ).strip()
            hosted_here_raw = os.environ.get(
                "DIGITORN_WORKER_HOSTED_MODULES", "",
            ).strip()
            hosted_here = {
                m.strip() for m in hosted_here_raw.split(",") if m.strip()
            }

            if inside_worker and "cron" not in hosted_here:
                # We're inside a worker but this one doesn't host
                # cron (e.g. the ``heavy`` worker also instantiated
                # cron for some reason). Stay idle.
                logger.info(
                    "cron_on_start_skipped reason=worker_%s_does_not_host_cron "
                    "pid=%d", inside_worker, os.getpid(),
                )
                return

            if not inside_worker:
                # We're in the daemon. Check whether the operator
                # has hosted cron on a worker via settings.workers.
                # If yes, defer to the worker. Settings are
                # consulted directly (the workers registry hasn't
                # been populated yet at ``lifecycle.start_all``).
                from digitorn.core.config import get_settings
                cfg = get_settings().workers
                if cfg.enabled and "cron" in cfg.hosted_module_names():
                    logger.info(
                        "cron_on_start_skipped_daemon_side reason=workered "
                        "pid=%d -- the worker will run the real on_start",
                        os.getpid(),
                    )
                    return
            # Else: we're inside the worker that hosts cron (the
            # normal case) -- fall through and acquire the lock.
        except Exception as exc:
            # Best-effort: if env / settings can't be read for any
            # reason, fall through to the normal acquire path.
            logger.debug("cron_skip_check_failed: %s", exc)

        # Late import to avoid forcing the workers package on every
        # module import (the workers config object is lightweight,
        # cron_lock pulls in OS primitives only on first use).
        from digitorn.workers.cron_lock import FileLeader, LeaderAcquireError

        lock_path = Path.home() / ".digitorn" / _LEADER_LOCK_FILENAME
        self._leader = FileLeader(lock_path)
        try:
            self._leader.acquire()
        except LeaderAcquireError as exc:
            logger.warning(
                "cron_leader_held_elsewhere reason=%s -- this cron "
                "instance stays idle (no sweep, no refresh)", exc,
            )
            self._leader = None
            return

        logger.info(
            "cron_leader_acquired pid=%d lock=%s sweep_interval_s=%.0f",
            os.getpid(), lock_path, self._sweep_interval_s,
        )

        self._stopping = False
        self._sweep_task = asyncio.create_task(
            self._sweep_loop(), name="cron-activation-sweep",
        )
        self._refresh_task = asyncio.create_task(
            self._leader_refresh_loop(), name="cron-leader-refresh",
        )

    async def on_stop(self) -> None:
        """Cancel tasks + release the lock. Idempotent."""
        self._stopping = True
        for task in (self._sweep_task, self._refresh_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._sweep_task = None
        self._refresh_task = None
        if self._leader is not None:
            try:
                self._leader.release()
            except Exception as exc:
                logger.debug("cron_leader_release_failed: %s", exc)
            self._leader = None
        logger.info("cron_module_stopped")

    # ── Background tasks ─────────────────────────────────────────

    async def _sweep_loop(self) -> None:
        """Run the activation sweep once every
        ``self._sweep_interval_s`` until ``on_stop`` cancels us.

        Ports the daemon's ``_sweep_iteration`` minus the in-memory
        rot detector (which depends on ``app.state.app_manager``
        and so cannot run in a worker process without a different
        data source). The rot detector continues to run daemon-side
        when cron is workered -- see ``server.py``'s guarded
        ``_activation_sweeper`` for the split.
        """
        while not self._stopping:
            try:
                await asyncio.sleep(self._sweep_interval_s)
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            await self._sweep_once()

    async def _sweep_once(self) -> None:
        try:
            from digitorn.core.app.activation_store import ActivationStore
            from digitorn.core.database import get_session_factory
        except Exception as exc:
            logger.debug(
                "cron_sweep_imports_unavailable: %s -- skipping iteration",
                exc,
            )
            return

        try:
            store = ActivationStore(get_session_factory())
        except Exception as exc:
            # DB not initialised (e.g. local mode without activations
            # table). Stay silent at DEBUG -- this is expected when
            # the worker boots before / without a backing DB.
            logger.debug(
                "cron_sweep_store_init_failed: %s -- skipping iteration",
                exc,
            )
            return

        try:
            n = await store.sweep_stuck_running(
                older_than_seconds=self._sweep_older_than_s,
            )
            if n:
                logger.info("cron_sweep marked_failed=%d", n)
        except Exception as exc:
            logger.debug("cron_sweep_iteration_failed: %s", exc)

    async def _leader_refresh_loop(self) -> None:
        """Bump the lock file's ``renewed_at`` every
        ``leader.renew_interval_s`` so a sibling worker doesn't
        detect us as stale and steal the lease.
        """
        if self._leader is None:
            return
        interval = self._leader.renew_interval_s
        while not self._stopping:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if self._stopping or self._leader is None:
                return
            try:
                self._leader.refresh()
            except Exception as exc:
                logger.warning("cron_leader_refresh_failed: %s", exc)
