"""Digitorn - Source watcher system.

Generic, event-driven change detection for any data source.
Ships with two backends:

    FilesystemWatcher  - native OS notifications (inotify/fsevents/kqueue)
                         via the `watchfiles` library (Rust `notify` crate).
    PollingWatcher     - hash-based fallback for databases, APIs, etc.

The SourceWatcherService orchestrates all active watchers and publishes
change events on the daemon event bus.
"""

from digitorn.core.watcher.types import ChangeEvent, ChangeType, WatchConfig, WatchMode

__all__ = ["ChangeType", "ChangeEvent", "WatchConfig", "WatchMode"]
