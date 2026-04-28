"""SourceWatcherService - orchestrator for all active watchers.

Central service that:
    1. Manages watcher lifecycle (start/stop per source).
    2. Multiplexes change events from all backends into the event bus.
    3. Provides a simple API for modules to register/unregister watches.

Integrates into the daemon lifespan and publishes events on topics:
    digitorn.watcher.{source_id}.file_created
    digitorn.watcher.{source_id}.file_modified
    digitorn.watcher.{source_id}.file_deleted
    digitorn.watcher.{source_id}.file_moved
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from digitorn.core.events.bus import EventBus
from digitorn.core.events.models import UniversalEvent
from digitorn.core.watcher.filesystem import FilesystemWatcher
from digitorn.core.watcher.polling import PollingWatcher
from digitorn.core.watcher.service_bus_poller import ServiceBusPollingWatcher
from digitorn.core.watcher.types import ChangeEvent, ChangeType, WatchConfig

logger = structlog.get_logger(__name__)

_TOPIC_SUFFIX = {
    ChangeType.CREATED: "file_created",
    ChangeType.MODIFIED: "file_modified",
    ChangeType.DELETED: "file_deleted",
    ChangeType.MOVED: "file_moved",
}


class SourceWatcherService:
    """Orchestrates watchers for all registered sources.

    Usage::

        service = SourceWatcherService(event_bus)
        await service.watch(WatchConfig(source_id="backend", root="/project", ...))
        await service.unwatch("backend")
        await service.shutdown()
    """

    _AnyWatcher = FilesystemWatcher | PollingWatcher | ServiceBusPollingWatcher

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._watchers: dict[str, SourceWatcherService._AnyWatcher] = {}
        self._configs: dict[str, WatchConfig] = {}
        self._consumers: dict[str, asyncio.Task[None]] = {}
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def watched_sources(self) -> list[str]:
        """Return list of currently watched source IDs."""
        return list(self._watchers.keys())

    async def start(self) -> None:
        """Start the watcher service."""
        self._running = True
        await logger.ainfo("watcher_service_started")

    async def shutdown(self) -> None:
        """Stop all watchers and clean up."""
        self._running = False
        source_ids = list(self._watchers.keys())
        for source_id in source_ids:
            await self.unwatch(source_id)
        await logger.ainfo("watcher_service_shutdown", stopped=len(source_ids))

    async def watch(
        self,
        config: WatchConfig,
        *,
        service_bus: Any = None,
        module_id: str | None = None,
    ) -> None:
        """Start watching a source.

        If a watcher already exists for this source_id, it is stopped
        and replaced with the new config.

        For non-filesystem sources, pass ``service_bus`` and ``module_id``
        to use the ServiceBusPollingWatcher which delegates ``list_items``
        and ``checksum`` calls to the owning module.
        """
        if config.source_id in self._watchers:
            await self.unwatch(config.source_id)

        backend: SourceWatcherService._AnyWatcher
        if config.backend == "service_bus" and service_bus and module_id:
            backend = ServiceBusPollingWatcher(config, service_bus, module_id)
        elif config.backend == "service_bus":
            await logger.awarning(
                "watcher_service_bus_unavailable",
                source_id=config.source_id,
                reason="service_bus or module_id not provided",
            )
            raise ValueError(
                f"service_bus backend for '{config.source_id}' requires "
                f"service_bus and module_id arguments"
            )
        elif config.backend == "polling":
            backend = PollingWatcher(config)
        else:
            backend = FilesystemWatcher(config)

        self._watchers[config.source_id] = backend
        self._configs[config.source_id] = config

        await backend.start()

        consumer = asyncio.create_task(
            self._consume(config.source_id, backend),
            name=f"watcher-consumer-{config.source_id}",
        )
        self._consumers[config.source_id] = consumer

        await logger.ainfo(
            "watcher_registered",
            source_id=config.source_id,
            backend=config.backend,
            root=config.root,
            mode=config.mode.value,
        )

    async def unwatch(self, source_id: str) -> bool:
        """Stop watching a source. Returns True if it was being watched."""
        consumer = self._consumers.pop(source_id, None)
        if consumer:
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass

        backend = self._watchers.pop(source_id, None)
        self._configs.pop(source_id, None)
        if backend:
            await backend.stop()
            await logger.ainfo("watcher_unregistered", source_id=source_id)
            return True

        return False

    def get_status(self) -> dict[str, Any]:
        """Get status of all active watchers."""
        watchers_info = {}
        for sid, w in self._watchers.items():
            config = self._configs.get(sid)
            watchers_info[sid] = {
                "backend": type(w).__name__,
                "running": w.running,
                "mode": config.mode.value if config else "unknown",
                "root": config.root if config else "",
            }
        return {
            "running": self._running,
            "watchers": watchers_info,
            "count": len(self._watchers),
        }


    async def _consume(
        self,
        source_id: str,
        backend: _AnyWatcher,
    ) -> None:
        """Consume events from a backend and publish to the event bus."""
        try:
            async for event in backend.changes():
                if not self._running:
                    break
                await self._publish_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await logger.aerror(
                "watcher_consumer_error",
                source_id=source_id,
                error=str(exc),
            )

    async def _publish_event(self, change: ChangeEvent) -> None:
        """Convert a ChangeEvent to a UniversalEvent and publish it."""
        suffix = _TOPIC_SUFFIX.get(change.change_type, "file_modified")
        topic = f"digitorn.watcher.{change.source_id}.{suffix}"

        event = UniversalEvent(
            topic=topic,
            source="watcher",
            event_type="watcher_change",
            data={
                "source_id": change.source_id,
                "path": change.path,
                "change_type": change.change_type.value,
                "content_hash": change.content_hash,
                "old_path": change.old_path,
                "metadata": change.metadata,
            },
        )

        await self._event_bus.publish(event)
