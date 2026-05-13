"""Worker subprocess lifecycle for the daemon's lifespan.

The daemon's FastAPI lifespan calls:

  * ``start_workers_if_enabled(app, settings)`` -- once during startup,
    after the rest of the daemon is ready. Reads
    ``settings.workers``; if empty / disabled, no-op. Otherwise
    spawns one ``digitorn-worker`` subprocess per declared worker,
    each bound to its configured port with its hosted-modules list
    passed via env. Returns a ``WorkerLifecycle`` handle.
  * ``WorkerLifecycle.stop()`` -- once during shutdown. Sends
    terminate to each worker, waits up to 10s, kills survivors.

Each worker is monitored by an asyncio task that:
  * polls ``GET /health`` every 5s,
  * restarts the worker on N consecutive failures (default 5 = 25s
    of unresponsiveness),
  * applies exponential backoff (2s, 5s, 15s, 30s, 60s capped).

When ``workers.enabled`` is False (default), this module does
nothing and the daemon runs as today.

Design notes
============

* Shared secret: the daemon reads / generates
  ``~/.digitorn/.workers-secret`` once at startup and exports it as
  ``DIGITORN_WORKERS_SECRET`` when spawning each worker. Both ends
  agree on the bearer token without further coordination.

* Logs: each worker's stdout / stderr is piped to a ring buffer +
  mirrored to the daemon's stderr with a ``[worker:<id>]`` prefix
  so a single ``tail -f`` sees the full picture.

* Cross-platform: uses ``asyncio.create_subprocess_exec`` rather
  than ``subprocess.Popen`` so the spawn itself never blocks the
  main loop. Termination via ``proc.terminate()`` works on both
  POSIX (SIGTERM) and Windows (TerminateProcess).
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import WorkerConfig, WorkersConfig

logger = logging.getLogger(__name__)


_HEALTH_INTERVAL_S = 5.0
_HEALTH_TIMEOUT_S = 3.0
_HEALTH_FAIL_LIMIT = 5
_RESTART_BACKOFF_S = [2.0, 5.0, 15.0, 30.0, 60.0]
_STOP_GRACE_S = 10.0
_STDOUT_RING_SIZE = 200


@dataclass
class _RunningWorker:
    cfg: WorkerConfig
    proc: asyncio.subprocess.Process
    monitor_task: asyncio.Task | None = None
    drain_task: asyncio.Task | None = None
    restart_count: int = 0
    consecutive_failures: int = 0
    stdout_ring: deque[str] = field(default_factory=lambda: deque(maxlen=_STDOUT_RING_SIZE))


class WorkerLifecycle:
    """Owns the set of running worker subprocesses.

    One instance per daemon. ``start()`` spawns + monitors;
    ``stop()`` terminates everything cleanly. Re-entrant only in the
    sense of being idempotent: a second ``stop()`` call is a no-op.
    """

    def __init__(
        self,
        workers_config: WorkersConfig,
        *,
        shared_secret: str,
    ) -> None:
        self._cfg = workers_config
        self._shared_secret = shared_secret
        self._running: dict[str, _RunningWorker] = {}
        self._stop_requested = False
        self._stopped = False

    @property
    def is_running(self) -> bool:
        return bool(self._running) and not self._stopped

    @property
    def worker_count(self) -> int:
        return len(self._running)

    async def start(self) -> None:
        """Spawn all workers declared in the config + launch monitors."""
        if not self._cfg.enabled or not self._cfg.workers:
            logger.debug("worker_lifecycle_skipped reason=disabled_or_empty")
            return
        for wcfg in self._cfg.workers:
            try:
                await self._spawn_worker(wcfg)
            except Exception as exc:
                logger.exception(
                    "worker_spawn_failed id=%s port=%d err=%s",
                    wcfg.id, wcfg.port, exc,
                )

    async def stop(self) -> None:
        """Terminate every worker. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        self._stop_requested = True

        # 1) Cancel the monitor tasks first so they don't try to
        #    restart workers we're killing.
        for rw in self._running.values():
            if rw.monitor_task and not rw.monitor_task.done():
                rw.monitor_task.cancel()

        # 2) Send terminate to every worker in parallel.
        for wid, rw in self._running.items():
            if rw.proc.returncode is None:
                try:
                    rw.proc.terminate()
                    logger.info("worker_terminate_sent id=%s pid=%s",
                                wid, rw.proc.pid)
                except ProcessLookupError:
                    pass
                except Exception as exc:
                    logger.warning(
                        "worker_terminate_failed id=%s err=%s", wid, exc,
                    )

        # 3) Wait for graceful exit, then kill survivors.
        async def _await_exit(rw: _RunningWorker) -> None:
            try:
                await asyncio.wait_for(rw.proc.wait(), timeout=_STOP_GRACE_S)
            except asyncio.TimeoutError:
                logger.warning(
                    "worker_kill_after_grace id=%s pid=%s grace_s=%.0f",
                    rw.cfg.id, rw.proc.pid, _STOP_GRACE_S,
                )
                try:
                    rw.proc.kill()
                except ProcessLookupError:
                    pass
                except Exception as exc:
                    logger.warning(
                        "worker_kill_failed id=%s err=%s",
                        rw.cfg.id, exc,
                    )

        await asyncio.gather(
            *(_await_exit(rw) for rw in self._running.values()),
            return_exceptions=True,
        )

        # 4) Drain tasks usually exit on EOF; cancel any stragglers.
        for rw in self._running.values():
            if rw.drain_task and not rw.drain_task.done():
                rw.drain_task.cancel()

        logger.info(
            "worker_lifecycle_stopped workers=%d",
            len(self._running),
        )
        self._running.clear()

    # ── Internals ────────────────────────────────────────────────

    async def _spawn_worker(self, wcfg: WorkerConfig) -> None:
        """Launch one ``digitorn-worker`` subprocess + start its
        monitor and stdout drain tasks.
        """
        # Prefer the installed CLI script (``digitorn-worker``) when
        # available -- it picks up the project's venv automatically.
        # Fallback to ``python -m digitorn.workers.app run`` so a
        # source-tree user without pip install -e still works.
        # NB: no "run" subcommand on the command line. Typer auto-
        # elides the single command's name (workers/app.py exposes
        # only ``run``), so passing "run" makes typer reject it with
        # "Got unexpected extra argument (run)". If we later add a
        # second command on the worker CLI, typer stops eliding and
        # we'll need to put "run" back here.
        digitorn_worker = shutil.which("digitorn-worker")
        if digitorn_worker:
            cmd = [
                digitorn_worker,
                "--id", wcfg.id,
                "--host", wcfg.host,
                "--port", str(wcfg.port),
                "--modules", ",".join(wcfg.modules),
            ]
        else:
            cmd = [
                sys.executable, "-m", "digitorn.workers.app",
                "--id", wcfg.id,
                "--host", wcfg.host,
                "--port", str(wcfg.port),
                "--modules", ",".join(wcfg.modules),
            ]

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        # Propagate the shared secret so the worker validates the
        # daemon's bearer token without reading the file again.
        env["DIGITORN_WORKERS_SECRET"] = self._shared_secret
        # Marker the spawned worker can read to know it is running
        # inside a worker process (vs the daemon). Modules that need
        # to behave differently daemon-side vs worker-side (e.g.
        # ``cron`` which must NOT acquire the leader lock daemon-side
        # but MUST acquire it inside the worker that hosts it) check
        # this env. Comma-separated list of modules this worker
        # hosts so the module can also verify its identity.
        env["DIGITORN_INSIDE_WORKER"] = wcfg.id
        env["DIGITORN_WORKER_HOSTED_MODULES"] = ",".join(wcfg.modules)

        logger.info(
            "worker_spawning id=%s port=%d modules=%s cmd=%s",
            wcfg.id, wcfg.port, wcfg.modules, cmd[0],
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        rw = _RunningWorker(cfg=wcfg, proc=proc)
        self._running[wcfg.id] = rw
        rw.drain_task = asyncio.create_task(
            self._drain_stdout(rw),
            name=f"worker-drain-{wcfg.id}",
        )
        rw.monitor_task = asyncio.create_task(
            self._monitor(rw),
            name=f"worker-monitor-{wcfg.id}",
        )

    async def _drain_stdout(self, rw: _RunningWorker) -> None:
        """Pipe worker stdout to the daemon's stderr (prefixed) + a
        ring buffer for incident captures. Async iteration => no
        blocking on the main loop.
        """
        assert rw.proc.stdout is not None
        try:
            async for raw in rw.proc.stdout:
                try:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                except Exception:
                    continue
                rw.stdout_ring.append(line)
                print(
                    f"[worker:{rw.cfg.id}] {line}",
                    file=sys.stderr, flush=True,
                )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("worker_drain_failed id=%s err=%s", rw.cfg.id, exc)

    async def _monitor(self, rw: _RunningWorker) -> None:
        """Poll the worker's /health every ``_HEALTH_INTERVAL_S`` and
        decide when to restart. Backoff on repeated restarts.
        """
        # Tiny grace period so we don't immediately probe a worker
        # that hasn't bound its port yet.
        await asyncio.sleep(1.0)

        while not self._stop_requested:
            # 1) Process alive?
            if rw.proc.returncode is not None:
                rc = rw.proc.returncode
                logger.warning(
                    "worker_died id=%s exit_code=%s -- restarting",
                    rw.cfg.id, rc,
                )
                await self._restart(rw, reason=f"exit_code_{rc}")
                continue

            # 2) Probe /health.
            ok = await self._probe_health(rw)
            if ok:
                rw.consecutive_failures = 0
            else:
                rw.consecutive_failures += 1
                if rw.consecutive_failures >= _HEALTH_FAIL_LIMIT:
                    logger.warning(
                        "worker_health_failed id=%s fails=%d -- restarting",
                        rw.cfg.id, rw.consecutive_failures,
                    )
                    await self._restart(rw, reason="health_unresponsive")
                    continue

            await asyncio.sleep(_HEALTH_INTERVAL_S)

    async def _probe_health(self, rw: _RunningWorker) -> bool:
        """``GET /health`` -- True on 200, False otherwise. httpx
        runs on the daemon loop, fully non-blocking.
        """
        try:
            import httpx
            async with httpx.AsyncClient(
                timeout=_HEALTH_TIMEOUT_S,
            ) as client:
                resp = await client.get(
                    f"http://{rw.cfg.host}:{rw.cfg.port}/health",
                )
                return resp.status_code == 200
        except Exception:
            return False

    async def _restart(self, rw: _RunningWorker, *, reason: str) -> None:
        """Kill (if needed) and re-spawn one worker."""
        if self._stop_requested:
            return

        # Kill the old process.
        if rw.proc.returncode is None:
            try:
                rw.proc.terminate()
            except Exception:
                pass
            try:
                await asyncio.wait_for(rw.proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    rw.proc.kill()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(rw.proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

        # Cancel the now-orphan drain task.
        if rw.drain_task and not rw.drain_task.done():
            rw.drain_task.cancel()

        # Backoff before respawn.
        idx = min(rw.restart_count, len(_RESTART_BACKOFF_S) - 1)
        delay = _RESTART_BACKOFF_S[idx]
        rw.restart_count += 1
        rw.consecutive_failures = 0
        logger.info(
            "worker_restart id=%s reason=%s backoff_s=%.0f restart_count=%d",
            rw.cfg.id, reason, delay, rw.restart_count,
        )
        await asyncio.sleep(delay)

        if self._stop_requested:
            return

        # Re-spawn. We drop the old _RunningWorker and replace it.
        self._running.pop(rw.cfg.id, None)
        try:
            await self._spawn_worker(rw.cfg)
        except Exception as exc:
            logger.exception(
                "worker_respawn_failed id=%s err=%s", rw.cfg.id, exc,
            )

    def stats(self) -> dict[str, Any]:
        """Snapshot for /health or admin endpoints."""
        return {
            "enabled": True,
            "worker_count": len(self._running),
            "workers": {
                wid: {
                    "port": rw.cfg.port,
                    "modules": rw.cfg.modules,
                    "pid": rw.proc.pid,
                    "alive": rw.proc.returncode is None,
                    "restart_count": rw.restart_count,
                    "consecutive_failures": rw.consecutive_failures,
                }
                for wid, rw in self._running.items()
            },
        }


# ── Module-level lifecycle owner -------------------------------------


_DEFAULT_LIFECYCLE: WorkerLifecycle | None = None


def get_default_lifecycle() -> WorkerLifecycle | None:
    return _DEFAULT_LIFECYCLE


async def start_workers_if_enabled(app: Any, settings: Any) -> WorkerLifecycle | None:
    """Daemon-side entry point. Called once during FastAPI startup.

    Returns the running ``WorkerLifecycle`` handle (or ``None`` when
    workers are disabled). The handle is also stored as
    ``app.state.worker_lifecycle`` so other components can introspect.

    Safe to call multiple times: idempotent via module-level singleton.
    """
    global _DEFAULT_LIFECYCLE

    if _DEFAULT_LIFECYCLE is not None and _DEFAULT_LIFECYCLE.is_running:
        return _DEFAULT_LIFECYCLE

    wcfg: WorkersConfig | None = None
    try:
        wcfg = settings.workers
    except Exception:
        return None

    if wcfg is None or not wcfg.enabled or not wcfg.workers:
        logger.debug(
            "worker_lifecycle_disabled enabled=%s workers=%d",
            getattr(wcfg, "enabled", None),
            len(getattr(wcfg, "workers", []) or []),
        )
        return None

    secret = _ensure_shared_secret()
    lifecycle = WorkerLifecycle(wcfg, shared_secret=secret)
    await lifecycle.start()
    _DEFAULT_LIFECYCLE = lifecycle

    try:
        app.state.worker_lifecycle = lifecycle
    except Exception:
        pass

    logger.info(
        "worker_lifecycle_ready spawned=%d ports=%s",
        lifecycle.worker_count,
        [w.port for w in wcfg.workers],
    )
    return lifecycle


async def stop_workers_if_running() -> None:
    """Daemon-side shutdown hook. Idempotent."""
    global _DEFAULT_LIFECYCLE
    if _DEFAULT_LIFECYCLE is None:
        return
    try:
        await _DEFAULT_LIFECYCLE.stop()
    except Exception as exc:
        logger.warning("worker_lifecycle_stop_error: %s", exc)
    finally:
        _DEFAULT_LIFECYCLE = None


def _ensure_shared_secret() -> str:
    """Read or generate the daemon/worker shared secret. Same file
    the worker reads at startup via ``app._load_shared_secret``.
    """
    secret_path = Path.home() / ".digitorn" / ".workers-secret"
    if secret_path.exists():
        try:
            value = secret_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass

    # Generate. The daemon owns the install-time creation; workers
    # treat absence as "regenerate" too, so a desync would surface as
    # an auth error rather than a silent mismatch.
    value = secrets.token_urlsafe(32)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(value, encoding="utf-8")
    try:
        os.chmod(secret_path, 0o600)
    except OSError:
        pass
    logger.info(
        "workers_secret_generated at=%s -- shared between daemon "
        "and worker subprocesses",
        secret_path,
    )
    return value
