"""WorkspaceCacheService: hot-path cache for per-session preview snapshots."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


# Keep in lockstep with WorkspaceModule._DISK_HYDRATE_SKIP_DIRS
_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".vite",
    ".cache",
    ".turbo",
    ".output",
    ".svelte-kit",
    ".digitorn",
    "target",
    ".pytest_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
})


@dataclass
class CacheEntry:
    """One cached snapshot plus the disk signature it was hydrated from."""

    snapshot: dict[str, Any]
    signatures: dict[str, tuple[int, int]] = field(default_factory=dict)
    git_signature: tuple[int, int] | None = None
    hydrated_at: float = 0.0


class WorkspaceCacheBackend(Protocol):
    """Pluggable storage."""

    def get(self, key: str) -> CacheEntry | None: ...
    def set(self, key: str, entry: CacheEntry) -> None: ...
    def delete(self, key: str) -> bool: ...
    def __contains__(self, key: str) -> bool: ...
    def __len__(self) -> int: ...


class InMemoryWorkspaceCacheBackend:
    """LRU in-process storage."""

    def __init__(self, max_size: int = 10_000) -> None:
        self._max_size = max_size
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()

    def get(self, key: str) -> CacheEntry | None:
        entry = self._store.get(key)
        if entry is not None:
            self._store.move_to_end(key)
        return entry

    def set(self, key: str, entry: CacheEntry) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = entry
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def delete(self, key: str) -> bool:
        return self._store.pop(key, None) is not None

    def __contains__(self, key: str) -> bool:
        return key in self._store

    def __len__(self) -> int:
        return len(self._store)


HydrateFn = Callable[[], Awaitable[dict[str, Any]]]


class WorkspaceCacheService:
    """Per-session preview cache with disk-sync guarantees."""

    def __init__(
        self,
        backend: WorkspaceCacheBackend | None = None,
        max_watchers: int = 1000,
    ) -> None:
        self._backend = backend or InMemoryWorkspaceCacheBackend()
        self._max_watchers = max_watchers
        self._watchers: OrderedDict[str, Any] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._watched_set: set[str] = set()

    async def get_or_hydrate(
        self,
        *,
        session_id: str,
        workspace_path: str,
        hydrate_fn: HydrateFn,
    ) -> dict[str, Any]:
        """Return a fresh snapshot, hydrating from disk only if needed."""
        if not session_id:
            return await hydrate_fn()

        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            cached = self._backend.get(session_id)

            if session_id in self._watched_set and cached is not None:
                self._watchers.move_to_end(session_id)
                return cached.snapshot

            if cached is not None and workspace_path:
                disk_sigs = await _scan_signatures(workspace_path)
                disk_git = _git_signature(workspace_path)
                if (
                    cached.signatures == disk_sigs
                    and cached.git_signature == disk_git
                ):
                    return cached.snapshot

            snapshot = await hydrate_fn()
            new_sigs = (
                await _scan_signatures(workspace_path) if workspace_path else {}
            )
            new_git = _git_signature(workspace_path) if workspace_path else None
            entry = CacheEntry(
                snapshot=snapshot,
                signatures=new_sigs,
                git_signature=new_git,
                hydrated_at=asyncio.get_event_loop().time(),
            )
            self._backend.set(session_id, entry)

            if workspace_path:
                self._maybe_start_watcher(session_id, workspace_path)

            return snapshot

    def invalidate(self, session_id: str) -> None:
        """Drop the entry and stop any FS watcher for this session."""
        if not session_id:
            return
        self._backend.delete(session_id)
        self._stop_watcher(session_id)
        self._locks.pop(session_id, None)

    def stats(self) -> dict[str, Any]:
        """Diagnostic counters for the admin endpoints."""
        return {
            "entries": len(self._backend),
            "watchers": len(self._watchers),
            "max_watchers": self._max_watchers,
        }

    def shutdown(self) -> None:
        """Stop every watcher."""
        for sid in list(self._watchers.keys()):
            self._stop_watcher(sid)

    def _maybe_start_watcher(self, session_id: str, workspace_path: str) -> None:
        if session_id in self._watched_set:
            self._watchers.move_to_end(session_id)
            return
        if not os.path.isdir(workspace_path):
            return

        try:
            from watchfiles import awatch  # noqa: F401
        except ImportError:
            return

        stop_event = asyncio.Event()
        try:
            task = asyncio.get_event_loop().create_task(
                self._watch_loop(session_id, workspace_path, stop_event),
                name=f"ws-cache-watch-{session_id}",
            )
        except RuntimeError:
            return

        self._watchers[session_id] = (task, stop_event)
        self._watched_set.add(session_id)

        while len(self._watchers) > self._max_watchers:
            try:
                oldest_sid, _ = self._watchers.popitem(last=False)
                self._watched_set.discard(oldest_sid)
                self._stop_watcher(oldest_sid, _from_lru=True)
            except KeyError:
                break

    async def _watch_loop(
        self, session_id: str, workspace_path: str, stop: asyncio.Event,
    ) -> None:
        try:
            from watchfiles import awatch
        except ImportError:
            return
        try:
            async for _changes in awatch(
                workspace_path,
                stop_event=stop,
                recursive=True,
                yield_on_timeout=False,
            ):
                try:
                    self._backend.delete(session_id)
                except Exception as exc:
                    logger.debug("workspace_cache best-effort block failed: %s", exc)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.debug(
                "workspace_watch_loop_failed sid=%s: %s",
                session_id, exc,
            )

    def _stop_watcher(self, session_id: str, *, _from_lru: bool = False) -> None:
        if _from_lru:
            handle = None
        else:
            handle = self._watchers.pop(session_id, None)
            self._watched_set.discard(session_id)
        if handle is None:
            return
        task, stop_event = handle
        try:
            stop_event.set()
        except Exception as exc:
            logger.debug("workspace_cache best-effort block failed: %s", exc)
        try:
            task.cancel()
        except Exception as exc:
            logger.debug("workspace_cache best-effort block failed: %s", exc)


async def _scan_signatures(workspace_path: str) -> dict[str, tuple[int, int]]:
    if not workspace_path or not os.path.isdir(workspace_path):
        return {}

    def _walk_blocking() -> dict[str, tuple[int, int]]:
        sigs: dict[str, tuple[int, int]] = {}
        base = Path(workspace_path)
        try:
            base_resolved = base.resolve()
        except Exception:
            base_resolved = base

        stack = [base_resolved]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            name = entry.name
                            if entry.is_dir(follow_symlinks=False):
                                if name in _SKIP_DIRS:
                                    continue
                                if name.startswith(".") and name != ".github":
                                    continue
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                stat = entry.stat(follow_symlinks=False)
                                rel = os.path.relpath(entry.path, base_resolved)
                                rel = rel.replace("\\", "/")
                                sigs[rel] = (stat.st_mtime_ns, stat.st_size)
                        except (FileNotFoundError, PermissionError):
                            continue
            except (FileNotFoundError, PermissionError, NotADirectoryError):
                continue
        return sigs

    return await asyncio.get_event_loop().run_in_executor(None, _walk_blocking)


def _git_signature(workspace_path: str) -> tuple[int, int] | None:
    if not workspace_path:
        return None
    try:
        git_dir = Path(workspace_path) / ".git"
        if not git_dir.is_dir():
            return None
        head_path = git_dir / "HEAD"
        index_path = git_dir / "index"
        try:
            head_mt = head_path.stat().st_mtime_ns
        except FileNotFoundError:
            head_mt = 0
        try:
            index_mt = index_path.stat().st_mtime_ns
        except FileNotFoundError:
            index_mt = 0
        return (head_mt, index_mt)
    except Exception:
        return None
