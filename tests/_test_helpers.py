"""Shared test utilities."""

from __future__ import annotations

import asyncio


def run_coro(coro):
    """Run a coroutine from sync code, safe even when an event loop is active.

    ``asyncio.run()`` fails if called from inside a running loop (common in
    pytest-asyncio suites).  This helper detects the situation and runs the
    coroutine in a background thread instead.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)
