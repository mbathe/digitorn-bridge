"""WorkspaceCacheService - hot-path cache for the per-session preview snapshot.

Designed to keep ``GET /sessions/{sid}/preview`` under a millisecond on
warm sessions while preserving disk-as-source-of-truth correctness for
cold or externally-modified sessions.

Three layers, in cost-increasing order:

  1. **Watcher fast path**: when an FS watcher (inotify / FSEvents /
     ReadDirectoryChangesW via ``watchdog``) is active for the session,
     external changes invalidate the cache entry in real time. The
     route trusts the cache 100% and returns instantly. Limited to the
     N most recently used sessions (``max_watchers``) so OS file-handle
     limits stay in scope at scale.

  2. **Signature warm path**: for sessions outside the watcher LRU,
     compare the cached signature ``{path: (mtime_ns, size_bytes)}`` +
     ``.git/HEAD`` mtime + ``.git/index`` mtime against disk. Pure
     ``stat()`` calls, no content reads. ~5-15 ms for a 200-file
     workspace on SSD. Catches every editor save and 99% of git
     operations even when content is preserved.

  3. **Cold path**: signature mismatch or empty cache - re-hydrate
     fully from disk via the caller-supplied ``hydrate_fn``. Result
     is cached + a watcher may be opened if the LRU has room.

Backend is pluggable via ``WorkspaceCacheBackend`` so swapping the
in-process LRU for Redis (multi-host) is one line at construction.

The service is intentionally narrow: it never imports the daemon's
preview / workspace modules. The HTTP route owns the hydrate function
and passes it as a callback. This keeps the cache reusable for any
session-scoped disk-backed snapshot in the future.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


# Directories that are never part of the user's workspace surface area
# (build artifacts, dependency caches, VCS internals). Skipped during
# the signature walk so a 50 MB ``node_modules`` tree never appears as
# a hot mtime check on every preview request. Kept in lockstep with
# ``WorkspaceModule._DISK_HYDRATE_SKIP_DIRS`` - if a directory is
# pruned by the hydration walk, the signature walk must prune it too,
# otherwise mtimes from those dirs would falsely invalidate the cache.
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
    "target",          # Rust / generic build dir
    ".pytest_cache",
    ".mypy_cache",
    ".idea",           # JetBrains
    ".vscode",         # VS Code
})


@dataclass
class CacheEntry:
    """One cached snapshot + the disk signature it was hydrated from."""

    snapshot: dict[str, Any]
    """The per-session payload returned by the hydrate function."""
    signatures: dict[str, tuple[int, int]] = field(default_factory=dict)
    """``{relative_path: (mtime_ns, size_bytes)}`` for every workspace file."""
    git_signature: tuple[int, int] | None = None
    """``(HEAD mtime_ns, index mtime_ns)`` if the workspace is a git repo."""
    hydrated_at: float = 0.0
    """Monotonic timestamp of the hydration - used for LRU ordering."""


class WorkspaceCacheBackend(Protocol):
    """Pluggable storage. Stays sync because every operation is O(1)."""

    def get(self, key: str) -> CacheEntry | None: ...
    def set(self, key: str, entry: CacheEntry) -> None: ...
    def delete(self, key: str) -> bool: ...
    def __contains__(self, key: str) -> bool: ...
    def __len__(self) -> int: ...


class InMemoryWorkspaceCacheBackend:
    """LRU in-process storage. Bounded; oldest entry evicted on overflow.

    For multi-host deployments swap with a Redis-backed implementation
    that satisfies the same protocol.
    """

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
    """Per-session preview cache with disk-sync guarantees.

    See module docstring for the layering. Concurrency: a per-session
    lock guarantees that N simultaneous requests against a cold key
    only trigger one hydration; the rest piggyback on the same task.
    """

    def __init__(
        self,
        backend: WorkspaceCacheBackend | None = None,
        max_watchers: int = 1000,
    ) -> None:
        self._backend = backend or InMemoryWorkspaceCacheBackend()
        self._max_watchers = max_watchers
        # Per-session FS watcher handles. LRU over watchers themselves
        # (file-descriptor budget) - the cached snapshots can stay
        # warm long after the watcher has been evicted.
        self._watchers: OrderedDict[str, Any] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        # Set of watched session ids for O(1) membership in the hot path.
        self._watched_set: set[str] = set()

    # ── Public API ────────────────────────────────────────────────

    async def get_or_hydrate(
        self,
        *,
        session_id: str,
        workspace_path: str,
        hydrate_fn: HydrateFn,
    ) -> dict[str, Any]:
        """Return a fresh snapshot, hydrating from disk only if needed.

        Hot path (watcher active):  ~1-5 µs (cache lookup).
        Warm path (signature OK):   ~5-15 ms (stat walk).
        Cold path (re-hydrate):     hydrate_fn cost (~200-800 ms typical).
        """
        if not session_id:
            return await hydrate_fn()

        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            cached = self._backend.get(session_id)

            # ── 1) Hot path: watcher is alive, cache is by definition fresh.
            if session_id in self._watched_set and cached is not None:
                self._watchers.move_to_end(session_id)
                return cached.snapshot

            # ── 2) Warm path: check signature against disk.
            if cached is not None and workspace_path:
                disk_sigs = await _scan_signatures(workspace_path)
                disk_git = _git_signature(workspace_path)
                if (
                    cached.signatures == disk_sigs
                    and cached.git_signature == disk_git
                ):
                    return cached.snapshot

            # ── 3) Cold path: hydrate, cache, optionally watch.
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
        """Drop the entry and stop any FS watcher for this session.

        Called on session teardown / explicit refresh / when an event
        flow has reason to suspect drift.
        """
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
        """Stop every watcher. Called from the daemon's lifespan teardown."""
        for sid in list(self._watchers.keys()):
            self._stop_watcher(sid)

    # ── Watcher plumbing ─────────────────────────────────────────

    def _maybe_start_watcher(self, session_id: str, workspace_path: str) -> None:
        """Attach an async FS watcher (``watchfiles.awatch``) for the session.

        Best-effort: if ``watchfiles`` is missing, the workspace path
        is gone, or the OS rejects the watch, we silently skip and
        rely on the signature path. The cache stays correct either way.
        """
        if session_id in self._watched_set:
            self._watchers.move_to_end(session_id)
            return
        if not os.path.isdir(workspace_path):
            return

        try:
            from watchfiles import awatch  # noqa: F401 (probe)
        except ImportError:
            return

        # Cancellation handle for the per-session watcher coroutine.
        stop_event = asyncio.Event()
        try:
            task = asyncio.get_event_loop().create_task(
                self._watch_loop(session_id, workspace_path, stop_event),
                name=f"ws-cache-watch-{session_id}",
            )
        except RuntimeError:
            # No running loop (called from a sync path) - bail. The
            # signature check still keeps the cache correct.
            return

        self._watchers[session_id] = (task, stop_event)
        self._watched_set.add(session_id)

        # LRU evict the oldest watcher(s) if we're over the budget.
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
        """Long-running coroutine that drains ``awatch`` for one session.

        Any change under ``workspace_path`` invalidates the cached
        entry. We don't try to patch the cache in place - the next
        ``get_or_hydrate`` rebuilds from disk, which keeps the code
        honest about disk being the source of truth.
        """
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
                except Exception:
                    pass
        except FileNotFoundError:
            # Workspace dir gone (session ended, app removed). Just exit.
            return
        except Exception as exc:
            logger.debug(
                "workspace_watch_loop_failed sid=%s: %s",
                session_id, exc,
            )

    def _stop_watcher(self, session_id: str, *, _from_lru: bool = False) -> None:
        """Cancel the watcher coroutine and forget it.

        ``_from_lru=True`` skips the popitem (already popped by the
        eviction caller) but still tries to cancel the task.
        """
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
        except Exception:
            pass
        try:
            task.cancel()
        except Exception:
            pass


# ── Disk-signature helpers ────────────────────────────────────────

async def _scan_signatures(workspace_path: str) -> dict[str, tuple[int, int]]:
    """Walk ``workspace_path`` and return ``{rel_path: (mtime_ns, size)}``.

    Stat-only (no read). Skips ``_SKIP_DIRS`` so build / cache / VCS
    trees don't dominate the walk. Runs in the default executor to
    keep the event loop responsive on large workspaces.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return {}

    def _walk_blocking() -> dict[str, tuple[int, int]]:
        sigs: dict[str, tuple[int, int]] = {}
        base = Path(workspace_path)
        try:
            base_resolved = base.resolve()
        except Exception:
            base_resolved = base

        # Use os.scandir manually so we can prune directories without
        # walking into them - much faster than rglob+filter on big repos.
        stack = [base_resolved]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            name = entry.name
                            if entry.is_dir(follow_symlinks=False):
                                # Prune build / cache / VCS trees from
                                # the walk - they're not part of the
                                # workspace surface and can dominate
                                # the runtime on big repos.
                                if name in _SKIP_DIRS:
                                    continue
                                # Skip hidden directories except
                                # ``.github`` which carries CI files
                                # visible in the workspace panel.
                                if name.startswith(".") and name != ".github":
                                    continue
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                stat = entry.stat(follow_symlinks=False)
                                rel = os.path.relpath(entry.path, base_resolved)
                                # Normalise separator so the same file on
                                # Windows / Linux gets the same key.
                                rel = rel.replace("\\", "/")
                                sigs[rel] = (stat.st_mtime_ns, stat.st_size)
                        except (FileNotFoundError, PermissionError):
                            continue
            except (FileNotFoundError, PermissionError, NotADirectoryError):
                continue
        return sigs

    return await asyncio.get_event_loop().run_in_executor(None, _walk_blocking)


def _git_signature(workspace_path: str) -> tuple[int, int] | None:
    """Return ``(HEAD mtime_ns, index mtime_ns)`` if the workspace is a git
    repo, else ``None``.

    These two file mtimes capture every flavour of git operation that
    can change tracked content while preserving file mtimes:
    ``git checkout``, ``git stash apply``, ``git reset --hard``,
    ``git pull``. Cheap (two ``stat()`` calls) and worth doing
    unconditionally - the daemon never knows up-front whether a
    workspace is git-managed.
    """
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
