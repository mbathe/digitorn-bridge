"""Per-session file routing for local backends."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Filenames of the form `<run_id>__<garbage>.tmp` that the router
# may emit during compaction. Keep these out of routine cleanup.
_INDEX_FILENAME = "_run_index.sqlite"


_BAD_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_SEGMENT_MAX = 96


def _sanitize_segment(s: str) -> str:
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
    return f"{_sanitize_segment(app_id)}/{_sanitize_segment(external_sid)}"


@dataclass
class _RunMapping:
    app_id: str
    external_sid: str
    rel: str  # `<app_id>/<external_sid>` already sanitized


class PerSessionRouter:
    """Owns the run_id → session-directory mapping for local backends."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache: dict[str, _RunMapping] = {}
        self._index_path = root / _INDEX_FILENAME
        self._index_lock = asyncio.Lock()


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


    def session_dir_for_start(
        self, run_id: str, app_id: str, external_sid: str,
    ) -> Path:
        """Register a new run and return its session directory."""
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
            logger.warning(
                "PerSessionRouter index write failed for run %s: %s",
                run_id, exc,
            )
        return path

    def session_dir_for_lookup(self, run_id: str) -> Optional[Path]:
        """Return the directory for an already-registered run, or None."""
        m = self._cache.get(run_id)
        if m is None:
            m = self._lookup_index(run_id)
            if m is None:
                return None
            self._cache[run_id] = m
        return self._root / m.rel

    def forget(self, run_id: str) -> None:
        """Drop the mapping after the run is fully terminal AND its"""
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
