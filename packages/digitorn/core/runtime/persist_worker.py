"""Off-loop persist worker."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


_MAX_QUEUE_DEPTH = 10_000

_DRAIN_POLL_INTERVAL_S = 0.5

# Max coroutines run concurrently per drain cycle. Larger = more
# throughput when the queue is deep, but more in-flight DB connections.
_BATCH_SIZE = 64


class PersistWorker:
    """Dedicated thread + asyncio loop for fire-and-forget DB writes."""

    def __init__(
        self,
        name: str = "persist-worker",
        max_depth: int = _MAX_QUEUE_DEPTH,
    ) -> None:
        self._name = name
        self._max_depth = max_depth
        # `queue.Queue` is fully thread-safe and sized; `put_nowait`
        # is what makes `submit` instantaneous on the main thread.
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
        self._worker_engine: Any = None
        self._worker_session_factory: Any = None


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
            self._started.wait(timeout=5.0)

    def submit(
        self,
        coro_factory: Callable[..., Awaitable[Any]],
        *args: Any, **kwargs: Any,
    ) -> bool:
        """Queue a coroutine factory for off-loop execution."""
        self.ensure_started()
        self._submitted += 1
        item = (coro_factory, args, kwargs)
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
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
        """Async variant of :meth:`run_sync`: run a coroutine on the"""
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
        """Run a coroutine on the worker's loop and BLOCK for the result."""
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


    def _run(self) -> None:
        """Thread entry point. Owns its own asyncio loop."""
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
                except Exception as exc:
                    logger.debug("persist_worker best-effort block failed: %s", exc)
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
        while not self._stop.is_set():
            try:
                batch = await asyncio.to_thread(self._drain_blocking)
            except RuntimeError as exc:
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
        """Pull at least one job (blocking) then up to `_BATCH_SIZE`"""
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
        """Lazy-build the worker's dedicated SQLAlchemy engine."""
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

        if is_postgres and "+psycopg" not in url:
            for old in ("postgresql+asyncpg://", "postgresql://"):
                if url.startswith(old):
                    url = "postgresql+psycopg://" + url[len(old):]
                    break

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
            connect_args["prepare_threshold"] = None

        pool_kwargs: dict[str, Any] = {}
        if is_sqlite:
            pool_kwargs["pool_pre_ping"] = True
        elif is_postgres:
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
    """Drain + stop the singleton. Called from the daemon's shutdown"""
    global _default_worker
    if _default_worker is not None:
        _default_worker.shutdown(timeout=timeout)
        _default_worker = None
