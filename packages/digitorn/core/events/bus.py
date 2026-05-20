"""Digitorn - Async event bus."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Coroutine

import structlog

from digitorn.core.events.models import UniversalEvent
from digitorn.core.events.router import EventRouter

logger = structlog.get_logger(__name__)

EventHandler = Callable[[UniversalEvent], Coroutine[Any, Any, None]]

class EventBus(ABC):
    """Abstract async event bus."""

    @abstractmethod
    async def publish(self, event: UniversalEvent) -> None:
        """Publish an event to all matching subscribers."""

    @abstractmethod
    def subscribe(self, pattern: str, handler: EventHandler) -> None:
        """Subscribe to events matching an MQTT-style topic pattern."""

    @abstractmethod
    def unsubscribe(self, pattern: str, handler: EventHandler) -> None:
        """Remove a subscription."""

class NullEventBus(EventBus):
    """No-op event bus. Silently discards all events."""

    async def publish(self, event: UniversalEvent) -> None:
        pass

    def subscribe(self, pattern: str, handler: EventHandler) -> None:
        pass

    def unsubscribe(self, pattern: str, handler: EventHandler) -> None:
        pass

class LogEventBus(EventBus):
    """Event bus that writes events as NDJSON to a log file."""

    def __init__(self, log_path: Path | str = "events.ndjson") -> None:
        self._log_path = Path(log_path)
        self._router = EventRouter()

    async def publish(self, event: UniversalEvent) -> None:
        line = event.model_dump_json() + "\n"
        try:
            await asyncio.to_thread(self._write_log, line)
        except Exception as exc:
            await logger.awarning("log_event_bus_write_error", error=str(exc))

        await self._dispatch(event)

    def _write_log(self, line: str) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a") as f:
            f.write(line)

    def subscribe(self, pattern: str, handler: EventHandler) -> None:
        self._router.add(pattern, handler)

    def unsubscribe(self, pattern: str, handler: EventHandler) -> None:
        self._router.remove(pattern, handler)

    async def _dispatch(self, event: UniversalEvent) -> None:
        handlers = self._router.match(event.topic)
        if handlers:
            await asyncio.gather(
                *(h(event) for h in handlers),
                return_exceptions=True,
            )

class FanoutEventBus(EventBus):
    """Dispatches events to multiple EventBus backends simultaneously."""

    def __init__(self, backends: list[EventBus] | None = None) -> None:
        self._backends: list[EventBus] = backends or []
        self._router = EventRouter()

    async def publish(self, event: UniversalEvent) -> None:
        results = await asyncio.gather(
            *(b.publish(event) for b in self._backends),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                await logger.awarning(
                    "event_bus_backend_error",
                    backend=type(self._backends[i]).__name__,
                    error=str(result),
                )

        handlers = self._router.match(event.topic)
        if handlers:
            await asyncio.gather(
                *(h(event) for h in handlers),
                return_exceptions=True,
            )

    def subscribe(self, pattern: str, handler: EventHandler) -> None:
        self._router.add(pattern, handler)

    def unsubscribe(self, pattern: str, handler: EventHandler) -> None:
        self._router.remove(pattern, handler)
