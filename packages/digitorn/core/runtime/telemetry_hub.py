"""Background telemetry collector for the daemon.

This module is the SINGLE-WRITER source of daemon-health metrics for
the admin dashboard. It runs ONE background asyncio task at 1 Hz that
samples psutil + in-memory state into a bounded deque. Admin endpoints
READ from the deque (microseconds); they never compute.

Isolation contract — never violated:

* No top-level import from any hot-path module. All cross-module reads
  happen via lazy ``try/import`` inside collector helpers; a failed
  import degrades that single metric to ``0`` and is logged once.
* psutil reads run through ``asyncio.to_thread`` — never on the loop.
* Each metric collector is wrapped in ``try/except``; one bad metric
  cannot kill the collection task.
* The collection task has a 2 s wall-clock cap per tick; if it
  exceeds, the tick is dropped and the next tick starts fresh.
* Subscribers (WebSocket consumers) receive snapshots via per-consumer
  ``asyncio.Queue`` with overflow drop-oldest. A slow consumer can
  never back-pressure the collector.

Wiring is OFF by default. ``install_telemetry(app)`` from the lifespan
starts the task; ``shutdown_telemetry()`` from the lifespan stops it.
The daemon runs identically with telemetry disabled.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


_COLLECT_INTERVAL_SECONDS = 1.0
_COLLECT_TIMEOUT_SECONDS = 2.0
_LAG_PROBE_INTERVAL_SECONDS = 0.1
_HISTORY_SIZE = 300
_SUBSCRIBER_QUEUE_SIZE = 16


@dataclass
class TelemetrySnapshot:
    """One tick of daemon health. All fields are optional / zero-safe
    so a partial collection still yields a valid snapshot.

    ``cpu_percent`` / ``rss_mb`` / ... are MAIN-process numbers only.
    ``workers[*]`` carries per-worker metrics. ``family_*`` carries the
    sum of main + every worker so the UI can show 'total daemon load'
    in one tile.
    """

    ts: float
    cpu_percent: float = 0.0
    rss_mb: float = 0.0
    vms_mb: float = 0.0
    num_threads: int = 0
    num_fds: int = 0
    loop_lag_ms: float = 0.0
    queue_depths: dict[str, int] = field(default_factory=dict)
    workers: list[dict[str, Any]] = field(default_factory=list)
    active_sessions: int = 0
    running_agents: int = 0
    db_pool: dict[str, Any] = field(default_factory=dict)
    family_cpu_percent: float = 0.0
    family_rss_mb: float = 0.0
    family_count: int = 0
    collect_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TelemetryHub:
    """Singleton owning the collector task + history deque + subscribers."""

    def __init__(self, app: Any | None = None) -> None:
        self._app = app
        self._snapshots: deque[TelemetrySnapshot] = deque(maxlen=_HISTORY_SIZE)
        self._subscribers: set[asyncio.Queue[TelemetrySnapshot]] = set()
        self._lag_ms: float = 0.0
        self._collect_task: asyncio.Task[None] | None = None
        self._lag_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._reported_import_failures: set[str] = set()
        # Cached psutil.Process — created once and reused. Critical for
        # cpu_percent: psutil computes the percentage as the delta between
        # consecutive calls on the SAME Process instance, so a fresh one
        # each tick always reports 0.0.
        self._psutil_proc: Any | None = None
        # Per-worker cache keyed by PID. Same delta-trick applies for
        # cpu_percent on workers: we keep the Process object alive across
        # ticks. Stale entries (worker died and got respawned with a new
        # PID) are evicted on each gather.
        self._psutil_workers: dict[int, Any] = {}

    async def start(self) -> None:
        if self._collect_task is not None and not self._collect_task.done():
            return
        self._stopping = False
        self._lag_task = asyncio.create_task(
            self._lag_loop(), name="telemetry_lag",
        )
        self._collect_task = asyncio.create_task(
            self._collect_loop(), name="telemetry_collect",
        )
        logger.info(
            "telemetry_hub_started interval=%.1fs history=%d",
            _COLLECT_INTERVAL_SECONDS, _HISTORY_SIZE,
        )

    async def stop(self) -> None:
        self._stopping = True
        for t in (self._collect_task, self._lag_task):
            if t is None or t.done():
                continue
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("telemetry_task_stop_warning: %s", exc)
        self._collect_task = None
        self._lag_task = None
        self._snapshots.clear()
        self._subscribers.clear()
        self._psutil_proc = None
        self._psutil_workers.clear()

    def current(self) -> TelemetrySnapshot | None:
        return self._snapshots[-1] if self._snapshots else None

    def history(self, seconds: int = 60) -> list[TelemetrySnapshot]:
        if not self._snapshots:
            return []
        cutoff = time.time() - max(1, seconds)
        return [s for s in self._snapshots if s.ts >= cutoff]

    def subscribe(self) -> asyncio.Queue[TelemetrySnapshot]:
        q: asyncio.Queue[TelemetrySnapshot] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_SIZE,
        )
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[TelemetrySnapshot]) -> None:
        self._subscribers.discard(q)

    async def _lag_loop(self) -> None:
        """Event-loop lag probe. Sleeps `_LAG_PROBE_INTERVAL_SECONDS`
        then measures how much the actual sleep exceeded the requested
        value. Anything > 50 ms is a real lag signal.

        Cost: one wake-up per 100 ms. Negligible.
        """
        target = _LAG_PROBE_INTERVAL_SECONDS
        try:
            while not self._stopping:
                t0 = time.monotonic()
                await asyncio.sleep(target)
                elapsed = time.monotonic() - t0
                self._lag_ms = max(0.0, (elapsed - target) * 1000.0)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("telemetry_lag_loop_died: %s", exc)

    async def _collect_loop(self) -> None:
        try:
            while not self._stopping:
                start = time.monotonic()
                snap: TelemetrySnapshot | None = None
                try:
                    snap = await asyncio.wait_for(
                        self._collect_one(),
                        timeout=_COLLECT_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning("telemetry_collect_timeout")
                except Exception as exc:
                    logger.warning("telemetry_collect_failed: %s", exc)

                if snap is not None:
                    snap.collect_ms = round(
                        (time.monotonic() - start) * 1000.0, 2,
                    )
                    self._snapshots.append(snap)
                    self._broadcast(snap)

                await asyncio.sleep(_COLLECT_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.exception("telemetry_collect_loop_died: %s", exc)

    async def _collect_one(self) -> TelemetrySnapshot:
        # The workers list is computed FIRST so ``_gather_psutil`` can
        # see the live PIDs and enrich each row with cpu_percent / rss_mb.
        # All heavy psutil work goes through ``to_thread`` in a single
        # batch so we cross the GIL boundary once per tick.
        workers = self._gather_workers()
        ps_data = await asyncio.to_thread(self._gather_psutil, workers)
        queues = self._gather_queue_depths()
        sessions, agents, approvals = self._gather_session_aggregates()
        if approvals is not None:
            queues["approvals"] = approvals
        db_pool = self._gather_db_pool()

        # Family totals = main + sum(workers). We count main even if a
        # worker metric failed (per-row try/except), so the sum reflects
        # what we COULD see.
        family_cpu = ps_data.get("cpu_percent", 0.0) + sum(
            float(w.get("cpu_percent", 0.0) or 0.0) for w in workers
        )
        family_rss = ps_data.get("rss_mb", 0.0) + sum(
            float(w.get("rss_mb", 0.0) or 0.0) for w in workers
        )
        family_count = 1 + sum(1 for w in workers if w.get("running"))

        return TelemetrySnapshot(
            ts=time.time(),
            cpu_percent=ps_data.get("cpu_percent", 0.0),
            rss_mb=ps_data.get("rss_mb", 0.0),
            vms_mb=ps_data.get("vms_mb", 0.0),
            num_threads=ps_data.get("num_threads", 0),
            num_fds=ps_data.get("num_fds", 0),
            loop_lag_ms=round(self._lag_ms, 2),
            queue_depths=queues,
            workers=workers,
            active_sessions=sessions,
            running_agents=agents,
            db_pool=db_pool,
            family_cpu_percent=round(family_cpu, 2),
            family_rss_mb=round(family_rss, 2),
            family_count=family_count,
            errors=ps_data.get("errors", []),
        )

    def _gather_psutil(self, workers: list[dict[str, Any]]) -> dict[str, Any]:
        """Per-metric ``try/except`` so one psutil failure (eg. an
        access-denied on num_fds) doesn't blank the whole row of
        tiles. Specific errors surface in the COLLECT ERRORS panel
        so we can see exactly which call broke.

        ``workers`` is mutated in place — each row gets ``cpu_percent``
        and ``rss_mb`` fields from its own ``psutil.Process(pid)``.
        Stale cache entries (worker respawned with a new PID) are
        evicted here.
        """
        out: dict[str, Any] = {"errors": []}
        try:
            import psutil
        except ImportError:
            out["errors"].append("psutil_not_installed")
            return out

        p = self._psutil_proc
        if p is None:
            try:
                p = psutil.Process(os.getpid())
                # Seed the internal _last_proc_times so the FIRST real
                # cpu_percent call (below) returns a meaningful delta.
                p.cpu_percent(interval=None)
                self._psutil_proc = p
            except Exception as exc:
                out["errors"].append(f"psutil_init:{type(exc).__name__}:{exc}")
                return out

        try:
            out["cpu_percent"] = float(p.cpu_percent(interval=None))
        except Exception as exc:
            out["errors"].append(f"cpu:{type(exc).__name__}:{exc}")

        try:
            mem = p.memory_info()
            out["rss_mb"] = round(mem.rss / 1_048_576, 2)
            out["vms_mb"] = round(mem.vms / 1_048_576, 2)
        except Exception as exc:
            out["errors"].append(f"mem:{type(exc).__name__}:{exc}")

        try:
            out["num_threads"] = int(p.num_threads())
        except Exception as exc:
            out["errors"].append(f"threads:{type(exc).__name__}:{exc}")

        try:
            out["num_fds"] = int(p.num_fds())
        except (AttributeError, NotImplementedError):
            try:
                out["num_fds"] = int(p.num_handles())
            except Exception as exc:
                out["errors"].append(f"fds:{type(exc).__name__}:{exc}")
        except Exception as exc:
            out["errors"].append(f"fds:{type(exc).__name__}:{exc}")

        # Enrich each worker row with cpu / rss aggregated across the
        # whole process tree (launcher + every descendant). On Windows
        # the PID we track is a thin launcher shim (~1 MB) that spawns
        # the real FastAPI worker as a child (50-200 MB); we must sum
        # both to surface the true footprint. We cache ``psutil.Process``
        # for every PID we touch so cpu_percent deltas are meaningful -
        # a fresh Process every tick always reports 0%.
        live_pids: set[int] = set()
        for w in workers:
            pid = int(w.get("pid") or 0)
            if pid <= 0 or not w.get("running"):
                continue
            wp = self._psutil_workers.get(pid)
            if wp is None:
                try:
                    wp = psutil.Process(pid)
                    wp.cpu_percent(interval=None)  # seed parent
                    self._psutil_workers[pid] = wp
                except Exception as exc:
                    out["errors"].append(
                        f"worker_init:{w.get('worker_id')}:{type(exc).__name__}",
                    )
                    continue

            cpu_total = 0.0
            rss_total = 0
            tree_pids: list[int] = [pid]
            try:
                cpu_total += float(wp.cpu_percent(interval=None))
                rss_total += int(wp.memory_info().rss)
                # Walk the full descendant tree once per tick. The
                # ``children(recursive=True)`` call is cheap on Windows
                # (job-object enumeration) and matches what Task Manager
                # shows under "Process tree" for the worker.
                for child in wp.children(recursive=True):
                    cpid = child.pid
                    tree_pids.append(cpid)
                    cwp = self._psutil_workers.get(cpid)
                    if cwp is None:
                        try:
                            cwp = psutil.Process(cpid)
                            cwp.cpu_percent(interval=None)  # seed child
                            self._psutil_workers[cpid] = cwp
                        except Exception:
                            continue
                    try:
                        cpu_total += float(cwp.cpu_percent(interval=None))
                        rss_total += int(cwp.memory_info().rss)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        self._psutil_workers.pop(cpid, None)
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                # Launcher itself died between gather_workers() and now.
                # Drop ALL cache entries for this tree so the next tick
                # re-discovers the respawn.
                for tp in tree_pids:
                    self._psutil_workers.pop(tp, None)
                out["errors"].append(
                    f"worker_vanished:{w.get('worker_id')}:{type(exc).__name__}",
                )
                continue
            except Exception as exc:
                out["errors"].append(
                    f"worker:{w.get('worker_id')}:{type(exc).__name__}",
                )
                continue

            w["cpu_percent"] = round(cpu_total, 2)
            w["rss_mb"] = round(rss_total / 1_048_576, 2)
            for tp in tree_pids:
                live_pids.add(tp)

        # Evict cache entries for PIDs no longer in any live tree (a
        # worker was restarted or a child exited).
        for stale_pid in list(self._psutil_workers.keys()):
            if stale_pid not in live_pids:
                self._psutil_workers.pop(stale_pid, None)

        return out

    def _gather_queue_depths(self) -> dict[str, int]:
        """Cheap module-level queue probes. Per-app queues (approvals,
        agent counts) are aggregated separately in
        ``_gather_session_aggregates`` which walks ``app_manager._deployed``.
        """
        out: dict[str, int] = {}
        try:
            from digitorn.core.runtime.run_tracker import worker as _wkr
            q = getattr(_wkr, "_queue", None)
            if q is not None:
                out["run_tracker"] = int(q.qsize())
        except Exception as exc:
            self._log_import_once("queue:run_tracker", exc)
        return out

    def _gather_workers(self) -> list[dict[str, Any]]:
        if self._app is None:
            return []
        try:
            lifecycle = getattr(self._app.state, "worker_lifecycle", None)
            if lifecycle is None:
                return []
            # Canonical attribute is ``_running: dict[str, _RunningWorker]``.
            # Each ``_RunningWorker`` wraps a ``proc`` (subprocess.Popen)
            # and a ``cfg`` (WorkerConfig). The subprocess is alive while
            # ``proc.returncode is None``.
            running = getattr(lifecycle, "_running", None) or {}
            out: list[dict[str, Any]] = []
            for wid, rw in running.items():
                proc = getattr(rw, "proc", None)
                cfg = getattr(rw, "cfg", None)
                is_alive = bool(
                    proc is not None and getattr(proc, "returncode", -1) is None
                )
                out.append({
                    "worker_id": wid,
                    "running": is_alive,
                    "pid": int(getattr(proc, "pid", 0) or 0) if proc else 0,
                    "port": int(getattr(cfg, "port", 0) or 0) if cfg else 0,
                })
            return out
        except Exception as exc:
            self._log_import_once("workers", exc)
        return []

    def _gather_session_aggregates(self) -> tuple[int, int, int | None]:
        """Sessions / running agents / pending approvals — all reads
        against ``app.state.app_manager._deployed`` which is the
        canonical map of live apps. Returns ``(sessions, agents,
        approvals_or_None)`` where ``approvals=None`` means we couldn't
        compute it (app_manager not yet ready) so the caller can omit
        the key rather than report a misleading zero.
        """
        sessions = 0
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            br = get_default_bridge()
            if br is not None:
                store = getattr(br, "store", None)
                inner = getattr(store, "_sessions", None) if store else None
                if inner is not None:
                    sessions = len(inner)
        except Exception as exc:
            self._log_import_once("sessions", exc)

        agents = 0
        approvals: int | None = None
        if self._app is None:
            return sessions, agents, approvals
        try:
            manager = getattr(self._app.state, "app_manager", None)
            if manager is None:
                return sessions, agents, approvals
            # Sessions: prefer the dispatcher's live task map (more
            # accurate than the SessionStore which retains idle ones).
            tasks = getattr(manager, "_session_tasks", None) or {}
            active = sum(
                1 for t in tasks.values() if t is not None and not t.done()
            )
            if active:
                sessions = active

            deployed = getattr(manager, "_deployed", None) or {}
            approvals_total = 0
            saw_any_approval_q = False
            for app in deployed.values():
                # Approvals: each DeployedApp owns one ApprovalQueue.
                aq = getattr(app, "approval_queue", None)
                pending = getattr(aq, "_pending", None) if aq else None
                if pending is not None:
                    saw_any_approval_q = True
                    try:
                        approvals_total += len(pending)
                    except Exception:
                        pass
                # Agents: each app has a Modules registry that may
                # include the agent_spawn module instance.
                modules = getattr(app, "modules", None) or {}
                spawn_mod = (
                    modules.get("agent_spawn")
                    if isinstance(modules, dict) else None
                )
                if spawn_mod is None:
                    continue
                bag = getattr(spawn_mod, "_agents", {}) or {}
                for per_sess in bag.values():
                    for a in per_sess.values():
                        t = getattr(a, "asyncio_task", None)
                        if t is not None and not t.done():
                            agents += 1
            if saw_any_approval_q:
                approvals = approvals_total
        except Exception as exc:
            self._log_import_once("aggregates", exc)
        return sessions, agents, approvals

    def _gather_db_pool(self) -> dict[str, Any]:
        try:
            from digitorn.core import database as _db
            engine = getattr(_db, "_engine", None)
            if engine is None:
                return {}
            pool = getattr(engine, "pool", None)
            if pool is None:
                return {}
            return {
                "size": int(getattr(pool, "size", lambda: 0)() or 0),
                "checked_out": int(
                    getattr(pool, "checkedout", lambda: 0)() or 0,
                ),
                "overflow": int(getattr(pool, "overflow", lambda: 0)() or 0),
            }
        except Exception as exc:
            self._log_import_once("db_pool", exc)
        return {}

    def _broadcast(self, snap: TelemetrySnapshot) -> None:
        if not self._subscribers:
            return
        for q in list(self._subscribers):
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(snap)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _log_import_once(self, key: str, exc: BaseException) -> None:
        if key in self._reported_import_failures:
            return
        self._reported_import_failures.add(key)
        logger.debug("telemetry_metric_unavailable kind=%s err=%s", key, exc)


_HUB: TelemetryHub | None = None


def get_hub() -> TelemetryHub | None:
    return _HUB


async def install_telemetry(app: Any | None = None) -> TelemetryHub:
    global _HUB
    if _HUB is not None:
        return _HUB
    _HUB = TelemetryHub(app=app)
    await _HUB.start()
    return _HUB


async def shutdown_telemetry() -> None:
    global _HUB
    if _HUB is None:
        return
    try:
        await _HUB.stop()
    finally:
        _HUB = None
