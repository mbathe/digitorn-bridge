"""Watcher types — shared data structures for all watcher backends."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ChangeType(StrEnum):
    """Kind of change detected on a source."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"


class WatchMode(StrEnum):
    """Lifecycle mode for a watcher.

    ``ephemeral``  — tied to the app/session, cleaned up on disconnect.
    ``persistent`` — saved to state, survives daemon restarts.
    """

    EPHEMERAL = "ephemeral"
    PERSISTENT = "persistent"


@dataclass(slots=True)
class ChangeEvent:
    """A single detected change.

    Emitted by watcher backends and forwarded to the event bus
    by the SourceWatcherService.
    """

    source_id: str
    path: str
    change_type: ChangeType
    timestamp: float = field(default_factory=time.time)
    content_hash: str | None = None
    old_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WatchConfig:
    """Configuration for watching a single source.

    Attributes:
        source_id:   Unique identifier matching the index source.
        backend:     Which backend to use: ``"filesystem"`` or ``"polling"``.
        root:        Root path or URI to watch.
        patterns:    Glob patterns to include (e.g. ``["**/*.py"]``).
        ignore:      Glob patterns to exclude (e.g. ``[".git/**", "__pycache__/**"]``).
        debounce_ms: Minimum interval between events for the same path (ms).
        poll_interval_s: Polling interval for PollingWatcher (seconds).
        metadata:    Extra config passed to the backend.
    """

    source_id: str
    backend: str = "filesystem"
    root: str = ""
    patterns: list[str] = field(default_factory=lambda: ["**/*"])
    ignore: list[str] = field(
        default_factory=lambda: [
            ".git/**",
            "__pycache__/**",
            "*.pyc",
            ".venv/**",
            "node_modules/**",
        ],
    )
    debounce_ms: int = 200
    poll_interval_s: float = 5.0
    mode: WatchMode = WatchMode.EPHEMERAL
    app_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence."""
        return {
            "source_id": self.source_id,
            "backend": self.backend,
            "root": self.root,
            "patterns": self.patterns,
            "ignore": self.ignore,
            "debounce_ms": self.debounce_ms,
            "poll_interval_s": self.poll_interval_s,
            "mode": self.mode.value,
            "app_id": self.app_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WatchConfig:
        """Deserialize from persistence."""
        return cls(
            source_id=data["source_id"],
            backend=data.get("backend", "filesystem"),
            root=data.get("root", ""),
            patterns=data.get("patterns", ["**/*"]),
            ignore=data.get("ignore", [".git/**", "__pycache__/**", "*.pyc", ".venv/**", "node_modules/**"]),
            debounce_ms=data.get("debounce_ms", 200),
            poll_interval_s=data.get("poll_interval_s", 5.0),
            mode=WatchMode(data.get("mode", "ephemeral")),
            app_id=data.get("app_id"),
            metadata=data.get("metadata", {}),
        )
