"""PollingWatcher - generic hash-based change detection.

Fallback backend for sources that don't support OS-native notifications:
databases, remote APIs, S3 buckets, etc.

Works by periodically computing content hashes and comparing with the
previous snapshot.  The hash function is pluggable via `metadata.hash_fn`.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import structlog

from digitorn.core.watcher.types import ChangeEvent, ChangeType, WatchConfig

logger = structlog.get_logger(__name__)

HashFn = Callable[[str], str | None]


def _default_file_hash(path: str) -> str | None:
    """SHA-256 hash of a file's content (first 16 hex chars)."""
    try:
        data = Path(path).read_bytes()
        return hashlib.sha256(data).hexdigest()[:16]
    except (OSError, PermissionError):
        return None


def _list_files(root: str, patterns: list[str], ignore: list[str]) -> list[str]:
    """List files under root matching patterns and not matching ignore."""
    import fnmatch

    root_path = Path(root)
    if not root_path.exists():
        return []

    results: list[str] = []
    for p in root_path.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = str(p.relative_to(root_path))
        except ValueError:
            continue

        skip = False
        for ig in ignore:
            if fnmatch.fnmatch(rel, ig) or fnmatch.fnmatch(p.name, ig):
                skip = True
                break
        if skip:
            continue

        for pat in patterns:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(p.name, pat):
                results.append(str(p))
                break

    return results


class PollingWatcher:
    """Watch a source by periodically hashing its contents.

    Suitable for any source type - filesystem, database rows, API endpoints.
    For non-filesystem sources, provide a custom `list_fn` and `hash_fn`
    in `config.metadata`.
    """

    def __init__(
        self,
        config: WatchConfig,
        *,
        hash_fn: HashFn | None = None,
        list_fn: Callable[[str, list[str], list[str]], list[str]] | None = None,
    ) -> None:
        self._config = config
        self._source_id = config.source_id
        self._running = False
        self._queue: asyncio.Queue[ChangeEvent] = asyncio.Queue(maxsize=4096)
        self._task: asyncio.Task[None] | None = None
        self._hash_fn = hash_fn or _default_file_hash
        self._list_fn = list_fn or _list_files
        self._snapshot: dict[str, str | None] = {}

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
        self._snapshot = await self._take_snapshot()
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"watcher-poll-{self._source_id}",
        )
        await logger.ainfo(
            "polling_watcher_started",
            source_id=self._source_id,
            files=len(self._snapshot),
            interval=self._config.poll_interval_s,
        )

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
        await logger.ainfo("polling_watcher_stopped", source_id=self._source_id)

    async def changes(self) -> AsyncIterator[ChangeEvent]:
        """Yield change events as they are detected."""
        while self._running or not self._queue.empty():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue


    async def _poll_loop(self) -> None:
        """Periodically compare snapshots to detect changes."""
        try:
            while self._running:
                await asyncio.sleep(self._config.poll_interval_s)
                if not self._running:
                    break

                new_snapshot = await self._take_snapshot()
                self._diff_and_emit(new_snapshot)
                self._snapshot = new_snapshot

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await logger.aerror(
                "polling_watcher_error",
                source_id=self._source_id,
                error=str(exc),
            )

    async def _take_snapshot(self) -> dict[str, str | None]:
        """Take a snapshot of all items and their hashes."""
        items = await asyncio.to_thread(
            self._list_fn,
            self._config.root,
            self._config.patterns,
            self._config.ignore,
        )
        snapshot: dict[str, str | None] = {}
        for item in items:
            h = await asyncio.to_thread(self._hash_fn, item)
            snapshot[item] = h
        return snapshot

    def _diff_and_emit(self, new: dict[str, str | None]) -> None:
        """Compare old and new snapshots, emit ChangeEvents for differences."""
        old = self._snapshot
        old_keys = set(old.keys())
        new_keys = set(new.keys())

        for path in new_keys - old_keys:
            self._emit(ChangeEvent(
                source_id=self._source_id,
                path=path,
                change_type=ChangeType.CREATED,
                content_hash=new[path],
            ))

        for path in old_keys - new_keys:
            self._emit(ChangeEvent(
                source_id=self._source_id,
                path=path,
                change_type=ChangeType.DELETED,
            ))

        for path in old_keys & new_keys:
            if old[path] != new[path]:
                self._emit(ChangeEvent(
                    source_id=self._source_id,
                    path=path,
                    change_type=ChangeType.MODIFIED,
                    content_hash=new[path],
                ))

    def _emit(self, event: ChangeEvent) -> None:
        """Put an event on the queue (drop oldest if full)."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(event)
