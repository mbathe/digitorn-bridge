"""Worker subprocess lifecycle for the daemon's lifespan."""
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
    """Owns the set of running worker subprocesses."""

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
        # Shared httpx client built lazily so the Windows cert-store
        # load happens once instead of on every health probe.
        self._health_client: Any = None
        self._health_client_lock = asyncio.Lock()

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

    async def wait_ready(self, *, timeout: float = 15.0) -> dict[str, bool]:
        """Poll `/health` on every spawned worker until it responds OK."""
        if not self._running:
            return {}
        import time as _time
        deadline = _time.monotonic() + max(1.0, timeout)
        # Poll every 250ms; 4 attempts/s is plenty for sub-second
        # readiness and bounded log spam.
        not_ready: dict[str, _RunningWorker] = dict(self._running)
        ready: dict[str, bool] = {}
        while not_ready and _time.monotonic() < deadline:
            for wid, rw in list(not_ready.items()):
                # Process died before becoming ready -- monitor will
                # restart it, but for this wait pass we give up on it.
                if rw.proc.returncode is not None:
                    ready[wid] = False
                    not_ready.pop(wid, None)
                    continue
                try:
                    ok = await self._probe_health(rw)
                except Exception:
                    ok = False
                if ok:
                    ready[wid] = True
                    not_ready.pop(wid, None)
            if not_ready:
                await asyncio.sleep(0.25)
        # Anything still not ready at deadline -> mark false, log loud.
        for wid in not_ready:
            ready[wid] = False
            logger.error(
                "worker_not_ready_after_timeout id=%s timeout_s=%.1f "
                "-- modules hosted on this worker will return "
                "transport errors until it comes up. Restarting auto-"
                "managed by the per-worker monitor.",
                wid, timeout,
            )
        return ready

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

        # 5) Close the shared probe client so its httpx connection
        # pool releases the loopback sockets immediately.
        if self._health_client is not None:
            try:
                await self._health_client.aclose()
            except Exception as exc:
                logger.debug("worker_health_client_close_err: %s", exc)
            self._health_client = None

        logger.info(
            "worker_lifecycle_stopped workers=%d",
            len(self._running),
        )
        self._running.clear()

    async def _spawn_worker(self, wcfg: WorkerConfig) -> None:
        # Prefer `digitorn-worker` (picks up the project venv); fall
        # back to `python -m digitorn.workers.app` for source-tree
        # users. Typer elides the single command name, so no `run`.
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
        # `DIGITORN_INSIDE_WORKER` lets modules behave differently
        # in a worker than in the daemon (cron leader-lock, etc.).
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
        try:
            client = await self._get_health_client()
            resp = await client.get(
                f"http://{rw.cfg.host}:{rw.cfg.port}/health",
                timeout=_HEALTH_TIMEOUT_S,
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def _get_health_client(self) -> Any:
        if self._health_client is not None:
            return self._health_client
        async with self._health_client_lock:
            if self._health_client is not None:
                return self._health_client
            import httpx

            def _build() -> Any:
                # Long-lived timeout per call comes from the
                # `client.get(timeout=...)` override. The constructor
                # only needs reasonable defaults.
                return httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_S)

            self._health_client = await asyncio.to_thread(_build)
            return self._health_client

    async def _restart(self, rw: _RunningWorker, *, reason: str) -> None:
        if self._stop_requested:
            return

        # Kill the old process.
        if rw.proc.returncode is None:
            try:
                rw.proc.terminate()
            except Exception as exc:
                logger.debug("lifecycle best-effort block failed: %s", exc)
            try:
                await asyncio.wait_for(rw.proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    rw.proc.kill()
                except Exception as exc:
                    logger.debug("lifecycle best-effort block failed: %s", exc)
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

_DEFAULT_LIFECYCLE: WorkerLifecycle | None = None

def get_default_lifecycle() -> WorkerLifecycle | None:
    return _DEFAULT_LIFECYCLE

async def start_workers_if_enabled(app: Any, settings: Any) -> WorkerLifecycle | None:
    """Daemon-side entry point. Called once during FastAPI startup."""
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
    except Exception as exc:
        logger.debug("lifecycle best-effort block failed: %s", exc)

    try:
        readiness = await lifecycle.wait_ready(timeout=15.0)
        ready_ids = [wid for wid, ok in readiness.items() if ok]
        not_ready_ids = [wid for wid, ok in readiness.items() if not ok]
        if not_ready_ids:
            logger.warning(
                "worker_lifecycle_partial_ready ready=%s not_ready=%s",
                ready_ids, not_ready_ids,
            )
        else:
            logger.info("worker_lifecycle_all_ready ids=%s", ready_ids)
    except Exception as exc:
        logger.warning(
            "worker_lifecycle_wait_ready_failed err=%s -- daemon "
            "continues, worker monitors will catch up",
            exc,
        )

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
