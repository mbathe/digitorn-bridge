"""Daemon worker pool: dedicated thread pools for heavy operations."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Coroutine, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_DEFAULT_TURN_WORKERS = min(16, (os.cpu_count() or 4) * 2)
_DEFAULT_IO_WORKERS = min(32, (os.cpu_count() or 4) * 4)

_thread_loops = threading.local()


class DaemonWorkerPool:
    """Manages thread pools for the daemon."""

    def __init__(
        self,
        max_turn_workers: int = _DEFAULT_TURN_WORKERS,
        max_io_workers: int = _DEFAULT_IO_WORKERS,
    ) -> None:
        self._turn_pool = ThreadPoolExecutor(
            max_workers=max_turn_workers,
            thread_name_prefix="agent-turn",
        )
        self._io_pool = ThreadPoolExecutor(
            max_workers=max_io_workers,
            thread_name_prefix="io",
        )
        self._active_turns = 0
        self._turns_lock = threading.RLock()
        self._max_turn_workers = max_turn_workers
        self._max_io_workers = max_io_workers
        self._shutting_down = False

        try:
            loop = asyncio.get_running_loop()
            loop.set_default_executor(self._io_pool)
            logger.info(
                "worker_pool_started turn_workers=%d io_workers=%d",
                max_turn_workers, max_io_workers,
            )
        except RuntimeError:
            logger.debug("worker_pool_created (loop not running yet)")

    def set_as_default_executor(self) -> None:
        """Set the I/O pool as asyncio default executor."""
        loop = asyncio.get_running_loop()
        loop.set_default_executor(self._io_pool)
        logger.info(
            "io_pool_set_as_default max_workers=%d", self._max_io_workers,
        )

    async def run_turn(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run an agent turn coroutine in the turn pool."""
        if self._shutting_down:
            raise RuntimeError("Worker pool is shutting down")

        with self._turns_lock:
            self._active_turns += 1
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._turn_pool,
                self._run_coro_in_thread,
                coro,
            )
        finally:
            with self._turns_lock:
                self._active_turns -= 1

    @staticmethod
    def _run_coro_in_thread(coro: Coroutine[Any, Any, T]) -> T:
        loop = getattr(_thread_loops, 'loop', None)
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            _thread_loops.loop = loop
        return loop.run_until_complete(coro)

    async def run_io(self, func: Callable[..., T], *args: Any) -> T:
        """Run a blocking I/O function in the I/O pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._io_pool, func, *args)

    @property
    def active_turns(self) -> int:
        """Number of agent turns currently running."""
        return self._active_turns

    @property
    def stats(self) -> dict[str, Any]:
        """Pool statistics for monitoring."""
        return {
            "turn_pool": {
                "max_workers": self._max_turn_workers,
                "active_turns": self._active_turns,
            },
            "io_pool": {
                "max_workers": self._max_io_workers,
            },
            "shutting_down": self._shutting_down,
        }

    async def shutdown(self, wait: bool = True, timeout: float = 30.0) -> None:
        """Gracefully shutdown both pools."""
        self._shutting_down = True
        logger.info(
            "worker_pool_shutdown active_turns=%d", self._active_turns,
        )

        if wait and self._active_turns > 0:
            deadline = asyncio.get_event_loop().time() + timeout
            while self._active_turns > 0:
                if asyncio.get_event_loop().time() > deadline:
                    logger.warning(
                        "worker_pool_shutdown_timeout remaining_turns=%d",
                        self._active_turns,
                    )
                    break
                await asyncio.sleep(0.5)

        self._turn_pool.shutdown(wait=False, cancel_futures=True)
        self._io_pool.shutdown(wait=False, cancel_futures=True)
        logger.info("worker_pool_stopped")


_pool: DaemonWorkerPool | None = None


def get_worker_pool() -> DaemonWorkerPool:
    """Get the global worker pool. Creates one if needed."""
    global _pool
    if _pool is None:
        _pool = DaemonWorkerPool()
    return _pool


def init_worker_pool(
    max_turn_workers: int = _DEFAULT_TURN_WORKERS,
    max_io_workers: int = _DEFAULT_IO_WORKERS,
) -> DaemonWorkerPool:
    """Initialize the global worker pool with custom settings."""
    global _pool
    if _pool is not None:
        logger.warning("worker_pool_already_initialized - reinitializing")
    _pool = DaemonWorkerPool(
        max_turn_workers=max_turn_workers,
        max_io_workers=max_io_workers,
    )
    return _pool


async def shutdown_worker_pool() -> None:
    """Shutdown the global worker pool."""
    global _pool
    if _pool is not None:
        await _pool.shutdown()
        _pool = None
