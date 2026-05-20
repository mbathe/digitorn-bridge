"""Active asyncio event-loop watchdog."""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_HEARTBEAT_INTERVAL_S = 0.2     # Coroutine bumps timestamp every 200ms.
_POLL_INTERVAL_S = 0.5          # OS thread polls every 500ms.
_DEFAULT_STALL_THRESHOLD_S = 2.0  # Alert if the loop hasn't ticked for 2s.
_COOLDOWN_S = 5.0               # Don't re-dump more than once every 5s.


class LoopWatchdog:
    def __init__(
        self,
        *,
        log_path: Path | None = None,
        stall_threshold_s: float = _DEFAULT_STALL_THRESHOLD_S,
    ) -> None:
        self._log_path = log_path or (
            Path.home() / ".digitorn" / "logs" / "loop-stall.log"
        )
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stall_threshold_s = stall_threshold_s
        self._last_heartbeat = time.monotonic()
        self._stop_evt = threading.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._poller_thread: threading.Thread | None = None
        self._last_dump_at = 0.0
        self._stalls_total = 0
        self._last_stall_gap_ms = 0.0

    def get_state(self) -> dict[str, Any]:
        return {
            "stalls_total": self._stalls_total,
            "last_stall_gap_ms": round(self._last_stall_gap_ms, 1),
            "threshold_s": self._stall_threshold_s,
            "log_path": str(self._log_path),
        }

    async def _heartbeat(self) -> None:
        try:
            while not self._stop_evt.is_set():
                self._last_heartbeat = time.monotonic()
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def _poll_loop(self) -> None:
        while not self._stop_evt.is_set():
            time.sleep(_POLL_INTERVAL_S)
            gap = time.monotonic() - self._last_heartbeat
            if gap < self._stall_threshold_s:
                continue
            now = time.monotonic()
            if now - self._last_dump_at < _COOLDOWN_S:
                continue
            self._last_dump_at = now
            self._stalls_total += 1
            self._last_stall_gap_ms = gap * 1000
            try:
                with open(self._log_path, "w", encoding="utf-8") as fh:
                    fh.write(
                        f"=== loop stall detected gap={gap:.2f}s "
                        f"threshold={self._stall_threshold_s:.2f}s "
                        f"ts={time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                    )
                    faulthandler.dump_traceback(file=fh, all_threads=True)
            except Exception as exc:
                logger.debug("loop_watchdog_dump_failed: %s", exc)
            logger.warning(
                "event_loop_stalled gap_s=%.2f threshold_s=%.2f "
                "stalls_total=%d - stack dumped to %s",
                gap, self._stall_threshold_s, self._stalls_total,
                self._log_path,
            )

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        loop.slow_callback_duration = 0.1
        self._heartbeat_task = loop.create_task(
            self._heartbeat(), name="loop-watchdog-heartbeat",
        )
        self._poller_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="loop-watchdog-poller",
        )
        self._poller_thread.start()
        logger.info(
            "loop_watchdog_started threshold_s=%.2f log_path=%s",
            self._stall_threshold_s, self._log_path,
        )

    def stop(self) -> None:
        self._stop_evt.set()
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        # Poller is a daemon thread; it'll exit on its next tick or on
        # process shutdown. We don't join() to avoid slowing shutdown.


_singleton: LoopWatchdog | None = None


def install(**kwargs: Any) -> LoopWatchdog:
    global _singleton
    if _singleton is None:
        _singleton = LoopWatchdog(**kwargs)
        _singleton.start()
    return _singleton


def get() -> LoopWatchdog | None:
    return _singleton
