"""ServiceBusPollingWatcher - module-delegated change detection.

For non-filesystem sources (databases, APIs, mail, S3, etc.), the watcher
cannot read content directly.  Instead, it delegates to the owning module
via the service bus using two standard actions:

    list_items(source_id, root, patterns)
        → {"items": [{"id": "users", "path": "public.users"}, ...]}

    checksum(source_id, items)
        → {"checksums": [{"id": "users", "hash": "abc123"}, ...]}

Any module that exposes these two actions is automatically watchable.
The watcher calls them periodically and diffs the results.

Contract for watchable modules
==============================

``list_items``
    Parameters:
        source_id (str): The registered source ID.
        root (str): Root URI/path of the source.
        patterns (list[str]): Filter patterns from the WatchConfig.
    Returns:
        {"items": [{"id": str, "path": str, ...}, ...]}

``checksum``
    Parameters:
        source_id (str): The registered source ID.
        items (list[str]): List of item IDs to checksum.
    Returns:
        {"checksums": [{"id": str, "hash": str | None}, ...]}
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import structlog

from digitorn.core.watcher.types import ChangeEvent, ChangeType, WatchConfig

logger = structlog.get_logger(__name__)


class ServiceBusPollingWatcher:
    """Watch a source by periodically querying its owning module via service bus.

    The module must expose ``list_items`` and ``checksum`` actions.
    """

    def __init__(
        self,
        config: WatchConfig,
        service_bus: Any,
        module_id: str,
    ) -> None:
        self._config = config
        self._source_id = config.source_id
        self._service_bus = service_bus
        self._module_id = module_id
        self._running = False
        self._queue: asyncio.Queue[ChangeEvent] = asyncio.Queue(maxsize=4096)
        self._task: asyncio.Task[None] | None = None
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
            self._poll_loop(), name=f"watcher-sbus-{self._source_id}",
        )
        await logger.ainfo(
            "service_bus_watcher_started",
            source_id=self._source_id,
            module_id=self._module_id,
            items=len(self._snapshot),
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
        await logger.ainfo("service_bus_watcher_stopped", source_id=self._source_id)

    async def changes(self) -> AsyncIterator[ChangeEvent]:
        """Yield change events as they are detected."""
        while self._running or not self._queue.empty():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue


    async def _poll_loop(self) -> None:
        """Periodically query the module and compare snapshots."""
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
                "service_bus_watcher_error",
                source_id=self._source_id,
                module_id=self._module_id,
                error=str(exc),
            )

    async def _take_snapshot(self) -> dict[str, str | None]:
        """Query the module for items and their checksums."""
        try:
            list_result = await self._service_bus.call(
                self._module_id,
                "list_items",
                {
                    "source_id": self._source_id,
                    "root": self._config.root,
                    "patterns": self._config.patterns,
                },
            )
        except Exception as exc:
            await logger.awarning(
                "service_bus_watcher_list_failed",
                source_id=self._source_id,
                module_id=self._module_id,
                error=str(exc),
            )
            return self._snapshot

        data = list_result.data if hasattr(list_result, "data") else list_result
        if not isinstance(data, dict):
            return self._snapshot

        items = data.get("items", [])
        if not items:
            return {}

        item_ids = [item["id"] if isinstance(item, dict) else str(item) for item in items]

        try:
            checksum_result = await self._service_bus.call(
                self._module_id,
                "checksum",
                {
                    "source_id": self._source_id,
                    "items": item_ids,
                },
            )
        except Exception as exc:
            await logger.awarning(
                "service_bus_watcher_checksum_failed",
                source_id=self._source_id,
                module_id=self._module_id,
                error=str(exc),
            )
            return self._snapshot

        cs_data = checksum_result.data if hasattr(checksum_result, "data") else checksum_result
        if not isinstance(cs_data, dict):
            return self._snapshot

        snapshot: dict[str, str | None] = {}
        for entry in cs_data.get("checksums", []):
            if isinstance(entry, dict):
                snapshot[entry["id"]] = entry.get("hash")

        for item_id in item_ids:
            if item_id not in snapshot:
                snapshot[item_id] = None

        return snapshot

    def _diff_and_emit(self, new: dict[str, str | None]) -> None:
        """Compare snapshots and emit ChangeEvents."""
        old = self._snapshot
        old_keys = set(old.keys())
        new_keys = set(new.keys())

        for item_id in new_keys - old_keys:
            self._emit(ChangeEvent(
                source_id=self._source_id,
                path=item_id,
                change_type=ChangeType.CREATED,
                content_hash=new[item_id],
            ))

        for item_id in old_keys - new_keys:
            self._emit(ChangeEvent(
                source_id=self._source_id,
                path=item_id,
                change_type=ChangeType.DELETED,
            ))

        for item_id in old_keys & new_keys:
            if old[item_id] != new[item_id]:
                self._emit(ChangeEvent(
                    source_id=self._source_id,
                    path=item_id,
                    change_type=ChangeType.MODIFIED,
                    content_hash=new[item_id],
                ))

    def _emit(self, event: ChangeEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(event)
