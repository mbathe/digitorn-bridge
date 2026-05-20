"""Watcher backend protocol - contract for all watcher implementations."""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from digitorn.core.watcher.types import ChangeEvent, WatchConfig


@runtime_checkable
class WatcherBackend(Protocol):
    """Protocol that every watcher backend must implement.

    A backend watches a single source and yields ChangeEvents
    as they are detected.  It is started/stopped by the
    SourceWatcherService.
    """

    @property
    def source_id(self) -> str:
        """Return the source_id this backend is watching."""
        ...

    @property
    def running(self) -> bool:
        """Whether the watcher is currently active."""
        ...

    async def start(self) -> None:
        """Start watching. Must be idempotent."""
        ...

    async def stop(self) -> None:
        """Stop watching and release resources. Must be idempotent."""
        ...

    def changes(self) -> AsyncIterator[ChangeEvent]:
        """Async iterator of change events.

        The iterator yields events as they are detected and
        blocks (awaits) when there is nothing new.  It should
        terminate cleanly when `stop()` is called.
        """
        ...
