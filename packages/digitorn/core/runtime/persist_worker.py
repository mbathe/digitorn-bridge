"""Off-loop persist worker.

Goal: the agent loop must NEVER wait for I/O. Even
``asyncio.create_task`` on the same event loop can starve when the
loop is congested (LLM tokenization, hook execution, GC sweep). The
solution is a dedicated thread that owns its own asyncio loop, its
own SQLAlchemy engine + connection pool, and a bounded thread-safe
job queue; the agent path just submits a job and returns instantly.

asyncpg cross-loop constraint
-----------------------------
asyncpg binds every connection to the loop that created it. A
naive worker that reuses the main daemon's session_factory crashes
with ``Future attached to a different loop`` on every query. The
fix: the worker creates its OWN engine + session_factory ON its
own loop, and uses a ``ContextVar`` override
(``database.set_session_factory_override``) to redirect every
``get_session_factory()`` call within its coroutines.

ContextVars propagate through ``await`` chains AND through
``asyncio.create_task``, so all 130+ call sites of
``get_session_factory()`` in the codebase automatically route to
the worker's factory when called from inside a worker job - no
parameter threading, no per-call-site changes.

Failure isolation
-----------------
* Worker thread crash → main loop unaffected; ensure_started()
  re-spawns on next submit (engine + factory recreated).
* Postgres unavailable → jobs accumulate up to ``max_depth``; once
  the cap hits, OLDEST jobs are dropped (warning logged). Memory
  growth bounded.
* Daemon shutdown → worker drains queue with timeout, disposes
  the engine cleanly before exiting.

Why not multiprocessing? ``ctx`` holds module instances + async
clients - all non-picklable. Thread + own-loop keeps memory access
intact while removing main-loop coupling.

Why not Redis as the queue? Redis adds external dep + IPC for
no benefit at single-daemon scale. Promoting to Redis is a one-
method swap when HA arrives - the public ``submit`` / ``shutdown``
API is intentionally storage-agnostic.

Architecture::

    ┌──────────────────────────┐         ┌─────────────────────┐
    │  Main daemon event loop  │         │  Persist thread     │
    │                          │         │  (own event loop)   │
    │  agent_turn(...)         │         │                     │
    │    │                     │         │  while True:        │
    │    └─ submit(coro_fact) ─│─────────│→  batch =           │
    │       (sync, microseconds)         │     queue.drain()   │
    │  loop continues immed.   │         │   await gather(     │
    └──────────────────────────┘         │     coro_fact(...)) │
                                         │                     │
                                         └─────────────────────┘

Failure isolation:

  * Worker thread crash → main loop unaffected; ensure_started()
    re-spawns on next submit.
  * Postgres unavailable → jobs accumulate up to ``max_depth``; once
    the cap hits, OLDEST jobs are dropped (warning logged). Memory
    growth bounded.
  * Daemon shutdown → worker drains queue with timeout before exit.

Why not multiprocessing? ``ctx`` holds module instances + async
clients - all non-picklable. Thread + own-loop keeps memory access
intact while removing main-loop coupling.

Why not Redis as the queue? Redis adds external dep + IPC for
no benefit at single-daemon scale. Promoting to Redis is a one-
method swap when HA arrives - the public ``submit`` / ``shutdown``
API is intentionally storage-agnostic.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# Default queue cap. Each entry holds a coroutine factory + a small
# args tuple (~few KB). 10k entries = ~50 MB worst case; in practice
# the worker drains far faster than turns can fill.
_MAX_QUEUE_DEPTH = 10_000

# How long the worker blocks on the queue before checking the stop
# flag. Smaller = faster shutdown response; larger = fewer wakeups.
# 0.5s is a good middle ground.
_DRAIN_POLL_INTERVAL_S = 0.5

# Max coroutines run concurrently per drain cycle. Larger = more
# throughput when the queue is deep, but more in-flight DB connections.
_BATCH_SIZE = 64


class PersistWorker:
    """Dedicated thread + asyncio loop for fire-and-forget DB writes.

    Public API (thread-safe, sync-callable from any coroutine):

      * ``submit(coro_factory, *args, **kwargs)`` -> bool
        Returns True when the job was accepted, False when the queue
        was full and an older job had to be dropped.

      * ``ensure_started()`` (idempotent boot)
      * ``shutdown(timeout=5.0)`` (drain + exit)
      * ``stats()`` -> dict

    ``submit`` takes a coroutine FACTORY (callable returning a coroutine),
    NOT a coroutine. Coroutines are bound to the loop that created them;
    the factory pattern lets the worker create the coroutine on its OWN
    loop, which is the prerequisite for off-loop execution.
    """

    def __init__(
        self,
        name: str = "persist-worker",
        max_depth: int = _MAX_QUEUE_DEPTH,
    ) -> None:
        self._name = name
        self._max_depth = max_depth
        # ``queue.Queue`` is fully thread-safe and sized; ``put_nowait``
        # is what makes ``submit`` instantaneous on the main thread.
        self._queue: queue.Queue[
            tuple[Callable[..., Awaitable[Any]], tuple, dict] | None
        ] = queue.Queue(maxsize=max_depth)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._started = threading.Event()
        # Best-effort counters. Plain int reads/writes are atomic in
        # CPython under the GIL so a lock isn't required.
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._dropped = 0
        self._crash_count = 0
        self._restart_lock = threading.Lock()
        # The dedicated engine + session_factory live HERE, created
        # on the worker thread's loop the first time a job runs.
        # Both must NEVER be touched from any other thread/loop -
        # asyncpg's connections are loop-bound.
        self._worker_engine: Any = None
        self._worker_session_factory: Any = None

    # ── Public API ─────────────────────────────────────────────────

    def ensure_started(self) -> None:
        """Boot the worker thread if not already running. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        with self._restart_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._started.clear()
            self._thread = threading.Thread(
                target=self._run, name=self._name, daemon=True,
            )
            self._thread.start()
            # Worker signals readiness from inside _run before it
            # starts pulling jobs; submit() works even before this
            # returns since the queue is already accepting puts.
            self._started.wait(timeout=5.0)

    def submit(
        self,
        coro_factory: Callable[..., Awaitable[Any]],
        *args: Any, **kwargs: Any,
    ) -> bool:
        """Queue a coroutine factory for off-loop execution.

        Synchronous, microsecond-class cost. Returns False when the
        queue was full and an older entry was dropped to make room
        (the new entry is still accepted; a previous one is lost).
        """
        self.ensure_started()
        self._submitted += 1
        item = (coro_factory, args, kwargs)
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            # Drop oldest, retry put. Best-effort - a concurrent
            # drain might have already emptied the queue, in which
            # case the put succeeds anyway.
            try:
                self._queue.get_nowait()
                self._dropped += 1
                if self._dropped % 100 == 1:
                    logger.warning(
                        "persist_worker queue full (cap=%d); dropped "
                        "%d oldest job(s) total. Postgres is likely "
                        "slow or unavailable.",
                        self._max_depth, self._dropped,
                    )
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                # Extremely unlikely - we just popped one.
                self._dropped += 1
            return False

    async def run_async(
        self,
        coro_factory: Callable[..., Awaitable[Any]],
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Async variant of :meth:`run_sync`: run a coroutine on the
        worker's loop and ``await`` the result from the caller's loop.

        Unlike ``run_sync`` (which blocks the calling thread), this
        method is safe to call from inside an asyncio coroutine on the
        main daemon loop -- the worker does the work, the main loop
        stays free to handle other tasks during the await.

        Use this for any READ from Postgres triggered by an HTTP /
        Socket.IO handler that otherwise would call
        ``get_session_factory()`` and ``await session.execute(...)``
        directly on the main loop -- which on Windows + asyncpg + SSL
        can produce 5-25s main-loop stalls (asyncpg cross-loop lock
        acquisition pathology). Routing through the worker isolates
        all asyncpg activity on its dedicated psycopg3 loop.
        """
        self.ensure_started()
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("persist_worker not running")

        async def _wrapped() -> Any:
            await self._ensure_engine()
            from digitorn.core.database import (
                set_session_factory_override,
                reset_session_factory_override,
            )
            token = set_session_factory_override(self._worker_session_factory)
            try:
                return await coro_factory(*args, **kwargs)
            finally:
                reset_session_factory_override(token)

        cfut = asyncio.run_coroutine_threadsafe(_wrapped(), self._loop)
        afut = asyncio.wrap_future(cfut)
        if timeout is not None:
            return await asyncio.wait_for(afut, timeout=timeout)
        return await afut

    def run_sync(
        self,
        coro_factory: Callable[..., Awaitable[Any]],
        *args: Any,
        timeout: float = 5.0,
        default: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Run a coroutine on the worker's loop and BLOCK for the result.

        Unlike :meth:`submit` (fire-and-forget), this returns the value
        produced by the coroutine. Use it from sync call-sites that need
        the result, e.g. seed-loaders, status checks, anything previously
        wrapping ``asyncio.run`` in a throwaway ThreadPoolExecutor.

        Why this exists: ``EventBuffer._load_seed_from_db`` and similar
        sync helpers used to spawn a new thread + new event loop per
        call, which created fresh asyncpg connections that crossed loop
        boundaries (root cause of the ``protocol state 3`` storms). By
        routing through the worker's single, dedicated psycopg3 loop we
        get pool reuse + driver immunity to connection rotation.

        On timeout, exception, or worker-not-alive: returns ``default``
        and logs at warning. Never raises.
        """
        self.ensure_started()
        if self._loop is None or not self._loop.is_running():
            return default

        async def _wrapped() -> Any:
            await self._ensure_engine()
            from digitorn.core.database import (
                set_session_factory_override,
                reset_session_factory_override,
            )
            token = set_session_factory_override(self._worker_session_factory)
            try:
                return await coro_factory(*args, **kwargs)
            finally:
                reset_session_factory_override(token)

        try:
            fut = asyncio.run_coroutine_threadsafe(_wrapped(), self._loop)
            return fut.result(timeout=timeout)
        except Exception as exc:
            logger.warning(
                "persist_worker run_sync failed: %s: %s",
                type(exc).__name__, exc,
            )
            return default

    def shutdown(self, timeout: float = 5.0) -> None:
        """Drain the queue then stop the worker thread."""
        if self._thread is None or not self._thread.is_alive():
            return
        self._stop.set()
        # Wake up the worker if it's blocked on queue.get.
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                "persist_worker did not drain within %.1fs - forcing "
                "exit; ~%d job(s) may be lost",
                timeout, self._queue.qsize(),
            )

    def stats(self) -> dict[str, Any]:
        return {
            "alive": self._thread is not None and self._thread.is_alive(),
            "depth": self._queue.qsize(),
            "max_depth": self._max_depth,
            "submitted": self._submitted,
            "completed": self._completed,
            "failed": self._failed,
            "dropped": self._dropped,
            "crashes": self._crash_count,
        }

    # ── Internals ──────────────────────────────────────────────────

    def _run(self) -> None:
        """Thread entry point. Owns its own asyncio loop.

        On Windows we force a ``SelectorEventLoop`` here even though
        the main daemon uses the default ``ProactorEventLoop``. Why:
        psycopg3's async driver is incompatible with ProactorEventLoop
        ("Psycopg cannot use the 'ProactorEventLoop' to run in async
        mode"). Since this worker only does Postgres I/O - never
        subprocess - the loss of subprocess support that comes with
        SelectorEventLoop costs us nothing here. The 64-fd `select()`
        cap is also a non-issue at the persist worker's scale (we
        hold ~6 connections max).
        """
        try:
            import sys
            if sys.platform == "win32":
                import selectors
                self._loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
            else:
                self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._started.set()
            try:
                self._loop.run_until_complete(self._main())
            finally:
                # Dispose the engine first so the pool releases its
                # asyncpg connections cleanly while the loop is still
                # alive. After ``loop.close()`` the dispose would
                # raise "Event loop is closed".
                try:
                    self._loop.run_until_complete(self._dispose_engine())
                except Exception as exc:
                    logger.debug(
                        "persist_worker: dispose during shutdown failed: %s",
                        exc,
                    )
                # Final cleanup: cancel any orphan tasks and close.
                try:
                    pending = asyncio.all_tasks(loop=self._loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        self._loop.run_until_complete(asyncio.gather(
                            *pending, return_exceptions=True,
                        ))
                except Exception:
                    pass
                self._loop.close()
                self._loop = None
        except Exception as exc:
            self._crash_count += 1
            logger.error(
                "persist_worker thread crashed (#%d): %s",
                self._crash_count, exc, exc_info=True,
            )
            self._thread = None
            self._loop = None
            self._started.clear()

    async def _main(self) -> None:
        """Worker main loop: pull batches from the queue and run them."""
        while not self._stop.is_set():
            # Block waiting for jobs in a separate OS thread so the
            # event loop stays responsive (e.g. for ensure_future
            # scheduling from coroutines we run).
            try:
                batch = await asyncio.to_thread(self._drain_blocking)
            except RuntimeError as exc:
                # Process is shutting down: ``asyncio.to_thread`` uses the
                # default ThreadPoolExecutor, which Python's atexit hook
                # shuts down BEFORE our worker thread observes ``_stop``.
                # The submit then raises ``RuntimeError: cannot schedule
                # new futures after shutdown``. Treat as clean shutdown
                # signal - drain any remaining items synchronously and
                # exit. Re-raise on any other RuntimeError so real bugs
                # surface.
                if "cannot schedule new futures after shutdown" in str(exc):
                    logger.debug(
                        "persist_worker: executor shut down during drain, "
                        "exiting cleanly",
                    )
                    self._stop.set()
                    break
                raise
            if batch is None:
                # Sentinel = shutdown; drain whatever is left non-blocking.
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        continue
                    await self._run_one(*item)
                break
            if not batch:
                continue
            # Run the batch concurrently, isolating failures.
            await asyncio.gather(
                *(self._run_one(f, a, kw) for f, a, kw in batch),
                return_exceptions=True,
            )

    def _drain_blocking(self) -> list | None:
        """Pull at least one job (blocking) then up to ``_BATCH_SIZE``
        more (non-blocking). Returns None on the shutdown sentinel."""
        try:
            head = self._queue.get(timeout=_DRAIN_POLL_INTERVAL_S)
        except queue.Empty:
            return []
        if head is None:
            return None
        batch: list = [head]
        while len(batch) < _BATCH_SIZE:
            try:
                more = self._queue.get_nowait()
            except queue.Empty:
                break
            if more is None:
                # Shutdown sentinel mixed in; remember to stop after.
                self._stop.set()
                break
            batch.append(more)
        return batch

    async def _ensure_engine(self) -> None:
        """Lazy-build the worker's dedicated SQLAlchemy engine.

        Driver choice (Postgres only):
          We FORCE psycopg3 (not asyncpg) for the worker's engine.
          The reason is the ``InternalClientError: got result for
          unknown protocol state 3`` bug that fires whenever Neon's
          PgBouncer rotates a connection between two pipelined
          asyncpg commands. asyncpg has no recovery path for it -
          the connection is poisoned, futures detach, and on Windows
          the proactor's GC sweep stalls the entire daemon.
          psycopg3 doesn't pipeline aggressively and gracefully
          handles connection swaps mid-transaction.

        URL is rewritten on the fly: ``+asyncpg://...`` becomes
        ``+psycopg://...`` so the user's config can stay unchanged.
        SQLite URLs pass through unchanged.

        ``prepare_threshold=None`` disables prepared-statement caching
        on psycopg3 - same role as ``statement_cache_size=0`` on
        asyncpg. Without it, PgBouncer can still hit "prepared
        statement does not exist" between connection swaps.
        """
        if self._worker_session_factory is not None:
            return
        from sqlalchemy.ext.asyncio import (
            AsyncSession, async_sessionmaker, create_async_engine,
        )
        from digitorn.core.config import get_settings
        settings = get_settings()
        url = settings.database.url
        is_sqlite = url.startswith("sqlite")
        is_postgres = "postgresql" in url

        # Rewrite the driver tag without disturbing the rest of the URL.
        # Tolerates ``postgresql://`` (default = psycopg2 sync, broken
        # for our async use) and ``postgresql+asyncpg://``.
        if is_postgres and "+psycopg" not in url:
            for old in ("postgresql+asyncpg://", "postgresql://"):
                if url.startswith(old):
                    url = "postgresql+psycopg://" + url[len(old):]
                    break

        # Translate asyncpg-style query params to libpq (psycopg3) syntax.
        # asyncpg accepts ``ssl=require``; psycopg/libpq wants ``sslmode=require``.
        # ``statement_cache_size`` is asyncpg-only; psycopg uses ``prepare_threshold``
        # via connect_args (set below). Drop it here.
        if is_postgres and "?" in url:
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            parts = urlsplit(url)
            qs = parse_qsl(parts.query, keep_blank_values=True)
            new_qs: list[tuple[str, str]] = []
            for k, v in qs:
                if k == "ssl":
                    val = v.lower()
                    mapping = {
                        "true": "require", "1": "require", "require": "require",
                        "prefer": "prefer", "allow": "allow",
                        "false": "disable", "0": "disable", "disable": "disable",
                        "verify-ca": "verify-ca", "verify-full": "verify-full",
                    }
                    new_qs.append(("sslmode", mapping.get(val, "require")))
                elif k in ("statement_cache_size", "server_settings"):
                    continue  # asyncpg-only
                else:
                    new_qs.append((k, v))
            url = urlunsplit((
                parts.scheme, parts.netloc, parts.path,
                urlencode(new_qs), parts.fragment,
            ))

        connect_args: dict[str, Any] = {}
        if is_sqlite:
            connect_args["check_same_thread"] = False
        elif is_postgres:
            # Disable server-side prepared-statement caching - the
            # equivalent of asyncpg's statement_cache_size=0 trick
            # for surviving PgBouncer's connection rotation.
            connect_args["prepare_threshold"] = None

        pool_kwargs: dict[str, Any] = {}
        if is_sqlite:
            pool_kwargs["pool_pre_ping"] = True
        elif is_postgres:
            # Persist is mostly sequential within a turn (assistant
            # message + tool results + checkpoint). 2+4 is enough for
            # bursty parallel-tool turns; leaves headroom under Neon's
            # free-tier 25-connection cap (5+10 main + 2+4 worker = 21).
            pool_kwargs.update(
                pool_size=2, max_overflow=4, pool_timeout=30,
                pool_recycle=300, pool_pre_ping=True,
            )

        self._worker_engine = create_async_engine(
            url,
            echo=settings.database.echo,
            connect_args=connect_args,
            **pool_kwargs,
        )
        self._worker_session_factory = async_sessionmaker(
            bind=self._worker_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        logger.info(
            "persist_worker: dedicated engine ready (driver=psycopg3, "
            "pool_size=%s, url=%s)",
            pool_kwargs.get("pool_size", "?"),
            url.split("@")[-1] if "@" in url else "(no-host)",
        )

    async def _dispose_engine(self) -> None:
        """Cleanly close the worker's engine (releases pool connections)."""
        eng = self._worker_engine
        if eng is None:
            return
        try:
            await eng.dispose()
        except Exception as exc:
            logger.debug("persist_worker engine_dispose_failed: %s", exc)
        finally:
            self._worker_engine = None
            self._worker_session_factory = None

    async def _run_one(
        self,
        coro_factory: Callable[..., Awaitable[Any]],
        args: tuple, kwargs: dict,
    ) -> None:
        # Make sure our dedicated engine exists. Lazy-build inside the
        # worker loop so connections are bound here, not on the main loop.
        try:
            await self._ensure_engine()
        except Exception as exc:
            self._failed += 1
            logger.warning(
                "persist_worker engine_init_failed: %s: %s",
                type(exc).__name__, exc,
            )
            return

        # Push the contextvar override so every ``get_session_factory()``
        # call inside this job's coroutine tree returns OUR factory.
        # ContextVars propagate through ``await`` and ``create_task`` so
        # nested coros see the same override transparently.
        from digitorn.core.database import (
            set_session_factory_override,
            reset_session_factory_override,
        )
        token = set_session_factory_override(self._worker_session_factory)
        try:
            await coro_factory(*args, **kwargs)
            self._completed += 1
        except Exception as exc:
            self._failed += 1
            # Don't propagate - we're fire-and-forget. Log at warning
            # so persistent issues are visible without spamming on
            # transient failures.
            logger.warning(
                "persist_worker job_failed: %s: %s",
                type(exc).__name__, exc,
            )
        finally:
            reset_session_factory_override(token)


# Module-level singleton.
_default_worker: PersistWorker | None = None


def get_default_worker() -> PersistWorker:
    """Return the process-wide singleton worker, creating it lazily."""
    global _default_worker
    if _default_worker is None:
        _default_worker = PersistWorker()
        _default_worker.ensure_started()
    return _default_worker


def shutdown_default_worker(timeout: float = 5.0) -> None:
    """Drain + stop the singleton. Called from the daemon's shutdown
    hook; safe to call multiple times. After this call,
    ``get_default_worker()`` will recreate a fresh worker if invoked
    again - useful in tests, harmless in production."""
    global _default_worker
    if _default_worker is not None:
        _default_worker.shutdown(timeout=timeout)
        _default_worker = None
