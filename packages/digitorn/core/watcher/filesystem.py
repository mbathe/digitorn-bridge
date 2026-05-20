"""FilesystemWatcher - native OS file-change detection.

Uses the `watchfiles` library (Rust `notify` crate underneath),
which provides:
    - inotify on Linux
    - FSEvents on macOS
    - ReadDirectoryChangesW on Windows

This is the primary, high-performance backend for filesystem sources.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
from pathlib import Path
from typing import AsyncIterator

import structlog

from digitorn.core.watcher.types import ChangeEvent, ChangeType, WatchConfig

logger = structlog.get_logger(__name__)

_CHANGE_MAP = {
    1: ChangeType.CREATED,
    2: ChangeType.MODIFIED,
    3: ChangeType.DELETED,
}


class FilesystemWatcher:
    """Watch a filesystem directory for changes using OS-native notifications.

    Events are buffered in an asyncio.Queue and consumed via `changes()`.
    """

    def __init__(self, config: WatchConfig) -> None:
        self._config = config
        self._source_id = config.source_id
        self._root = Path(config.root).resolve()
        self._running = False
        self._queue: asyncio.Queue[ChangeEvent] = asyncio.Queue(maxsize=4096)
        self._task: asyncio.Task[None] | None = None
        self._debounce_s = config.debounce_ms / 1000.0

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._watch_loop(), name=f"watcher-fs-{self._source_id}")
        await logger.ainfo("filesystem_watcher_started", source_id=self._source_id, root=str(self._root))

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await logger.ainfo("filesystem_watcher_stopped", source_id=self._source_id)

    async def changes(self) -> AsyncIterator[ChangeEvent]:
        """Yield change events as they arrive."""
        while self._running or not self._queue.empty():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue


    async def _watch_loop(self) -> None:
        """Core watch loop using watchfiles.awatch."""
        import watchfiles

        try:
            async for changes in watchfiles.awatch(
                self._root,
                debounce=max(100, self._config.debounce_ms),
                stop_event=self._make_stop_event(),
                rust_timeout=5000,
                yield_on_timeout=True,
            ):
                if not self._running:
                    break

                for change_type_int, path_str in changes:
                    if not self._matches(path_str):
                        continue

                    change_type = _CHANGE_MAP.get(change_type_int)
                    if not change_type:
                        continue

                    content_hash = None
                    if change_type != ChangeType.DELETED:
                        content_hash = self._hash_file(path_str)

                    event = ChangeEvent(
                        source_id=self._source_id,
                        path=path_str,
                        change_type=change_type,
                        content_hash=content_hash,
                    )

                    try:
                        self._queue.put_nowait(event)
                    except asyncio.QueueFull:
                        try:
                            self._queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        self._queue.put_nowait(event)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await logger.aerror(
                "filesystem_watcher_error",
                source_id=self._source_id,
                error=str(exc),
            )

    def _make_stop_event(self) -> asyncio.Event:
        """Create an event that gets set when the watcher stops."""
        stop = asyncio.Event()

        async def _check_running() -> None:
            while self._running:
                await asyncio.sleep(0.5)
            stop.set()

        asyncio.create_task(_check_running(), name=f"watcher-stop-{self._source_id}")
        return stop

    def _matches(self, path_str: str) -> bool:
        """Check if a path matches include patterns and doesn't match ignore patterns."""
        try:
            rel_path = Path(path_str).resolve().relative_to(self._root)
            rel = str(rel_path)
            name = rel_path.name
        except ValueError:
            return False

        for pattern in self._config.ignore:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                return False

        for pattern in self._config.patterns:
            if (
                fnmatch.fnmatch(rel, pattern)
                or fnmatch.fnmatch(name, pattern)
                or (pattern.startswith("**/") and fnmatch.fnmatch(name, pattern[3:]))
            ):
                return True

        return False

    @staticmethod
    def _hash_file(path_str: str) -> str | None:
        """Compute SHA-256 hash of a file's content."""
        try:
            data = Path(path_str).read_bytes()
            return hashlib.sha256(data).hexdigest()[:16]
        except (OSError, PermissionError):
            return None
