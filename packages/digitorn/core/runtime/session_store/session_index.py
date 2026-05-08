"""Cross-session index: query sessions by user, app, time range.

The SessionStore + jsonfile/blob layers handle ONE session at a time
in O(1). They don't answer ``what are this user's last 20 chats?``
without walking every directory. This module fills that gap with a
tiny indexed view over the per-session metadata.

Default backend: SQLite (stdlib only, no async DB driver). Sync calls
are wrapped in ``asyncio.to_thread`` so the event loop is never
blocked. ~150 bytes per row -> 100k sessions = ~15 MB on disk.

Index is **derived state** -- the source of truth is the per-session
``meta.json`` + ``snapshot.json``. If the index is wiped, it can be
rebuilt by walking the sessions root and upserting each summary.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

logger = logging.getLogger(__name__)


@dataclass
class SessionSummary:
    """The light row stored in the index. Mirrors ``state.summary()``
    plus a few fields useful for the dashboard."""

    session_id: str
    app_id: str
    user_id: str
    parent_session_id: str | None = None
    started_at: str = ""
    ended_at: str | None = None
    closed: bool = False
    last_seq: int = 0
    event_count: int = 0
    cost_total: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    title: str | None = None
    summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "app_id": self.app_id,
            "user_id": self.user_id,
            "parent_session_id": self.parent_session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "closed": self.closed,
            "last_seq": self.last_seq,
            "event_count": self.event_count,
            "cost_total": self.cost_total,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "title": self.title,
            "summary": self.summary,
        }

    @classmethod
    def from_state_summary(cls, d: dict[str, Any]) -> "SessionSummary":
        return cls(
            session_id=str(d["session_id"]),
            app_id=str(d.get("app_id", "")),
            user_id=str(d.get("user_id", "")),
            parent_session_id=d.get("parent_session_id"),
            started_at=str(d.get("started_at", "")),
            ended_at=d.get("ended_at"),
            closed=bool(d.get("closed", False)),
            last_seq=int(d.get("last_seq", 0)),
            event_count=int(d.get("event_count", 0)),
            cost_total=float(d.get("cost_total", 0.0)),
            tokens_in=int(d.get("tokens_in", 0)),
            tokens_out=int(d.get("tokens_out", 0)),
            title=d.get("title"),
            summary=d.get("summary"),
        )


class SessionIndex(Protocol):
    async def upsert(self, summary: SessionSummary) -> None: ...
    async def get(self, session_id: str) -> SessionSummary | None: ...
    async def list_for_user(
        self, user_id: str, *,
        app_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_archived_children: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionSummary]: ...
    async def list_for_app(
        self, app_id: str, *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionSummary]: ...
    async def list_children(
        self, parent_session_id: str,
    ) -> list[SessionSummary]: ...
    async def search_titles(
        self, user_id: str, query: str, *, limit: int = 20,
    ) -> list[SessionSummary]: ...
    async def delete(self, session_id: str) -> bool: ...
    async def count_for_user(self, user_id: str) -> int: ...


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    app_id              TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    parent_session_id   TEXT,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    closed              INTEGER NOT NULL DEFAULT 0,
    last_seq            INTEGER NOT NULL DEFAULT 0,
    event_count         INTEGER NOT NULL DEFAULT 0,
    cost_total          REAL NOT NULL DEFAULT 0,
    tokens_in           INTEGER NOT NULL DEFAULT 0,
    tokens_out          INTEGER NOT NULL DEFAULT 0,
    title               TEXT,
    summary             TEXT
);

CREATE INDEX IF NOT EXISTS ix_sessions_user_started
    ON sessions (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_sessions_user_app
    ON sessions (user_id, app_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_sessions_app
    ON sessions (app_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_sessions_parent
    ON sessions (parent_session_id);
"""


_COLUMNS = (
    "session_id", "app_id", "user_id", "parent_session_id",
    "started_at", "ended_at", "closed", "last_seq", "event_count",
    "cost_total", "tokens_in", "tokens_out", "title", "summary",
)


def _row_to_summary(row: sqlite3.Row) -> SessionSummary:
    return SessionSummary(
        session_id=row["session_id"],
        app_id=row["app_id"],
        user_id=row["user_id"],
        parent_session_id=row["parent_session_id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        closed=bool(row["closed"]),
        last_seq=int(row["last_seq"]),
        event_count=int(row["event_count"]),
        cost_total=float(row["cost_total"]),
        tokens_in=int(row["tokens_in"]),
        tokens_out=int(row["tokens_out"]),
        title=row["title"],
        summary=row["summary"],
    )


class SqliteSessionIndex:
    """SQLite-backed cross-session index. Stdlib only.

    Concurrency: SQLite holds a process-wide single-writer-at-a-time
    lock; multiple async readers are fine. All ops go through
    ``asyncio.to_thread`` so the event loop is never blocked even
    on a slow disk.
    """

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._setup()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _setup(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    async def upsert(self, summary: SessionSummary) -> None:
        await asyncio.to_thread(self._upsert_sync, summary)

    def _upsert_sync(self, s: SessionSummary) -> None:
        cols_csv = ", ".join(_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _COLUMNS)
        update_csv = ", ".join(
            f"{c}=excluded.{c}" for c in _COLUMNS if c != "session_id"
        )
        sql = (
            f"INSERT INTO sessions ({cols_csv}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(session_id) DO UPDATE SET {update_csv}"
        )
        with self._connect() as conn:
            conn.execute(sql, {
                "session_id": s.session_id,
                "app_id": s.app_id,
                "user_id": s.user_id,
                "parent_session_id": s.parent_session_id,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "closed": int(s.closed),
                "last_seq": s.last_seq,
                "event_count": s.event_count,
                "cost_total": s.cost_total,
                "tokens_in": s.tokens_in,
                "tokens_out": s.tokens_out,
                "title": s.title,
                "summary": s.summary,
            })
            conn.commit()

    async def get(self, session_id: str) -> SessionSummary | None:
        return await asyncio.to_thread(self._get_sync, session_id)

    def _get_sync(self, session_id: str) -> SessionSummary | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return _row_to_summary(row) if row else None

    async def list_for_user(
        self,
        user_id: str,
        *,
        app_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_archived_children: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionSummary]:
        return await asyncio.to_thread(
            self._list_for_user_sync,
            user_id, app_id, since, until,
            include_archived_children, limit, offset,
        )

    def _list_for_user_sync(
        self, user_id: str, app_id: str | None,
        since: str | None, until: str | None,
        include_archived_children: bool,
        limit: int, offset: int,
    ) -> list[SessionSummary]:
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if app_id is not None:
            clauses.append("app_id = ?")
            params.append(app_id)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("started_at <= ?")
            params.append(until)
        if not include_archived_children:
            clauses.append("parent_session_id IS NULL")
        where = " AND ".join(clauses)
        sql = (
            f"SELECT * FROM sessions WHERE {where} "
            f"ORDER BY started_at DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_summary(r) for r in rows]

    async def list_for_app(
        self, app_id: str, *,
        limit: int = 50, offset: int = 0,
    ) -> list[SessionSummary]:
        return await asyncio.to_thread(
            self._list_for_app_sync, app_id, limit, offset,
        )

    def _list_for_app_sync(
        self, app_id: str, limit: int, offset: int,
    ) -> list[SessionSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE app_id = ? "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (app_id, limit, offset),
            ).fetchall()
            return [_row_to_summary(r) for r in rows]

    async def list_children(
        self, parent_session_id: str,
    ) -> list[SessionSummary]:
        return await asyncio.to_thread(
            self._list_children_sync, parent_session_id,
        )

    def _list_children_sync(
        self, parent_session_id: str,
    ) -> list[SessionSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE parent_session_id = ? "
                "ORDER BY started_at ASC",
                (parent_session_id,),
            ).fetchall()
            return [_row_to_summary(r) for r in rows]

    async def search_titles(
        self, user_id: str, query: str, *, limit: int = 20,
    ) -> list[SessionSummary]:
        return await asyncio.to_thread(
            self._search_titles_sync, user_id, query, limit,
        )

    def _search_titles_sync(
        self, user_id: str, query: str, limit: int,
    ) -> list[SessionSummary]:
        like = f"%{query}%"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions "
                "WHERE user_id = ? AND ("
                "  title LIKE ? OR summary LIKE ?"
                ") "
                "ORDER BY started_at DESC LIMIT ?",
                (user_id, like, like, limit),
            ).fetchall()
            return [_row_to_summary(r) for r in rows]

    async def delete(self, session_id: str) -> bool:
        return await asyncio.to_thread(self._delete_sync, session_id)

    def _delete_sync(self, session_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    async def count_for_user(self, user_id: str) -> int:
        return await asyncio.to_thread(self._count_for_user_sync, user_id)

    def _count_for_user_sync(self, user_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return int(row["n"]) if row else 0

    async def rebuild_from_summaries(
        self, summaries: list[SessionSummary],
    ) -> int:
        """Wipe + re-populate. Used to recover the index from the
        per-session ``meta.json`` files when the SQLite file is lost
        or corrupted."""
        return await asyncio.to_thread(
            self._rebuild_sync, summaries,
        )

    def _rebuild_sync(self, summaries: list[SessionSummary]) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions")
            n = 0
            for s in summaries:
                self._upsert_with_conn(conn, s)
                n += 1
            conn.commit()
        return n

    @staticmethod
    def _upsert_with_conn(
        conn: sqlite3.Connection, s: SessionSummary,
    ) -> None:
        cols_csv = ", ".join(_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _COLUMNS)
        update_csv = ", ".join(
            f"{c}=excluded.{c}" for c in _COLUMNS if c != "session_id"
        )
        sql = (
            f"INSERT INTO sessions ({cols_csv}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(session_id) DO UPDATE SET {update_csv}"
        )
        conn.execute(sql, {
            "session_id": s.session_id,
            "app_id": s.app_id,
            "user_id": s.user_id,
            "parent_session_id": s.parent_session_id,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
            "closed": int(s.closed),
            "last_seq": s.last_seq,
            "event_count": s.event_count,
            "cost_total": s.cost_total,
            "tokens_in": s.tokens_in,
            "tokens_out": s.tokens_out,
            "title": s.title,
            "summary": s.summary,
        })
