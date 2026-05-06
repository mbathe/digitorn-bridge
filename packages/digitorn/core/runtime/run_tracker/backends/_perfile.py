"""Per-session file routing for local backends.

Why this exists:

    The first cut of the local backends (``sqlite``, ``kv``,
    ``jsonfile``) wrote every event into ONE file under
    ``~/.digitorn/`` regardless of which session emitted it. Two
    drawbacks:

      1. The file grows unbounded. Six months of multi-user history
         in one ``runs.kv`` becomes unmanageable.
      2. Concurrent writes from N parallel sessions all serialise on
         a single fcntl lock (kv) or SQLite write-lock. The runtime
         gets the throughput of one writer.

    By routing each session into its own file under
    ``<root>/<app_id>/<external_sid>/agent_runs.<ext>``, two parallel
    sessions land in two different files - the OS schedules them
    independently. Deleting a session's data is one ``rm -rf``.

The router maintains a ``run_id -> session_dir`` cache. ``start_run``
populates it (the snapshot has ``app_id`` + ``session_id``);
subsequent ops (``emit_event``, ``complete_run``, increments) lookup
by ``run_id``. The cache is mirrored to a tiny SQLite index file at
the root so the mapping survives a daemon restart - if the worker
crashes between ``start_run`` and ``complete_run``, the next boot
loads the index and the in-flight runs reconnect to their files.

Index footprint: one row per active run. Completed runs can be
GC'd from the index after a configurable TTL; until that's wired,
the index grows roughly with total runs ever started. Even at 1M
runs that's a single ~50 MB file the daemon never reads on hot
paths (only at boot to populate the cache).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Filenames of the form ``<run_id>__<garbage>.tmp`` that the router
# may emit during compaction. Keep these out of routine cleanup.
_INDEX_FILENAME = "_run_index.sqlite"


# Sanitization rules for path segments. Filesystems disagree on what
# characters are valid (Windows is the strictest), so the safe subset
# is "letters, digits, dash, underscore, dot". Everything else gets
# replaced; segments are then truncated to keep total path length
# under typical 255-byte filename limits.
_BAD_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_SEGMENT_MAX = 96


def _sanitize_segment(s: str) -> str:
    """Make a path segment safe on every filesystem we ship to."""
    if not s:
        return "_"
    cleaned = _BAD_CHARS.sub("_", s).strip("._")
    if not cleaned:
        return "_"
    if len(cleaned) > _SEGMENT_MAX:
        # Keep a hash suffix to avoid collisions when truncating long
        # ids (e.g. the daemon's hex UUID session ids).
        import hashlib
        digest = hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
        cleaned = cleaned[: _SEGMENT_MAX - 9] + "-" + digest
    return cleaned


def _session_relpath(app_id: str, external_sid: str) -> str:
    """Build the relative directory for one session's data files."""
    return f"{_sanitize_segment(app_id)}/{_sanitize_segment(external_sid)}"


@dataclass
class _RunMapping:
    app_id: str
    external_sid: str
    rel: str  # ``<app_id>/<external_sid>`` already sanitized


class PerSessionRouter:
    """Owns the run_id → session-directory mapping for local backends.

    Each backend creates a router pointing at its own root
    (``~/.digitorn/runs/sqlite``, ``~/.digitorn/runs/kv``, etc.) and
    delegates path resolution to it. The router never opens the
    actual data files; it just hands back ``Path`` objects.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache: dict[str, _RunMapping] = {}
        self._index_path = root / _INDEX_FILENAME
        self._index_lock = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────────────

    async def setup(self) -> None:
        await asyncio.to_thread(self._open_and_load)

    async def teardown(self) -> None:
        # Index file is closed eagerly per write; nothing to release.
        return

    def _open_and_load(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._index_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_index (
                    run_id TEXT PRIMARY KEY,
                    app_id TEXT NOT NULL,
                    external_sid TEXT NOT NULL,
                    rel TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_run_index_session "
                "ON run_index(app_id, external_sid)"
            )
            conn.commit()
            for run_id, app_id, external_sid, rel in conn.execute(
                "SELECT run_id, app_id, external_sid, rel FROM run_index"
            ):
                self._cache[run_id] = _RunMapping(app_id, external_sid, rel)
        logger.info(
            "PerSessionRouter loaded %d run mappings from %s",
            len(self._cache), self._index_path,
        )

    # ── routing ────────────────────────────────────────────────────

    def session_dir_for_start(
        self, run_id: str, app_id: str, external_sid: str,
    ) -> Path:
        """Register a new run and return its session directory.

        Creates the directory on disk so callers can immediately open
        a file under it. Persists the mapping to the index so the
        next process can reconnect.
        """
        rel = _session_relpath(app_id, external_sid)
        path = self._root / rel
        path.mkdir(parents=True, exist_ok=True)
        self._cache[run_id] = _RunMapping(app_id, external_sid, rel)
        # Write-through to the index. Per-call SQLite open is cheap
        # (~1ms) and avoids holding a connection across loops.
        try:
            with sqlite3.connect(self._index_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO run_index "
                    "(run_id, app_id, external_sid, rel) VALUES (?, ?, ?, ?)",
                    (run_id, app_id, external_sid, rel),
                )
                conn.commit()
        except Exception as exc:
            # Index failure is non-fatal - the in-memory cache still
            # lets THIS process route correctly. Restart safety is
            # the only thing we lose. Logged so the operator can fix
            # disk-full or perms issues.
            logger.warning(
                "PerSessionRouter index write failed for run %s: %s",
                run_id, exc,
            )
        return path

    def session_dir_for_lookup(self, run_id: str) -> Optional[Path]:
        """Return the directory for an already-registered run, or None."""
        m = self._cache.get(run_id)
        if m is None:
            # Slow path: the run was registered before this process
            # booted AND the cache load missed it (e.g., the run was
            # added by another concurrent process). Fetch from index.
            m = self._lookup_index(run_id)
            if m is None:
                return None
            self._cache[run_id] = m
        return self._root / m.rel

    def forget(self, run_id: str) -> None:
        """Drop the mapping after the run is fully terminal AND its
        events have been flushed. Lets the index stay bounded.
        Caller decides when this is safe (usually after complete_run).
        """
        self._cache.pop(run_id, None)
        try:
            with sqlite3.connect(self._index_path) as conn:
                conn.execute("DELETE FROM run_index WHERE run_id = ?", (run_id,))
                conn.commit()
        except Exception as exc:
            logger.debug(
                "PerSessionRouter forget failed for run %s: %s", run_id, exc,
            )

    def _lookup_index(self, run_id: str) -> Optional[_RunMapping]:
        try:
            with sqlite3.connect(self._index_path) as conn:
                cur = conn.execute(
                    "SELECT app_id, external_sid, rel FROM run_index "
                    "WHERE run_id = ?",
                    (run_id,),
                )
                row = cur.fetchone()
        except Exception:
            return None
        if not row:
            return None
        return _RunMapping(row[0], row[1], row[2])
