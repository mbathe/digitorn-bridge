"""SQLite backend for the local mode (one DB file per session)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from digitorn.core.runtime.run_tracker.backends._perfile import PerSessionRouter

logger = logging.getLogger(__name__)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    app_id              TEXT NOT NULL,
    external_sid        TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    parent_run_id       TEXT,
    status              TEXT NOT NULL DEFAULT 'queued',
    status_reason       TEXT,
    specialist          TEXT,
    provider            TEXT,
    model               TEXT,
    max_turns           INTEGER,
    turns_used          INTEGER NOT NULL DEFAULT 0,
    sub_agents_spawned  INTEGER NOT NULL DEFAULT 0,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    task_summary        TEXT,
    queued_at           TEXT NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    last_event_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_agent_runs_status
    ON agent_runs(status);
CREATE INDEX IF NOT EXISTS ix_agent_runs_started
    ON agent_runs(started_at);

CREATE TABLE IF NOT EXISTS agent_run_events (
    run_id      TEXT NOT NULL,
    sequence    INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',
    elapsed_ms  INTEGER,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
CREATE INDEX IF NOT EXISTS ix_agent_run_events_run_created
    ON agent_run_events(run_id, created_at);
"""

_DB_FILENAME = "runs.sqlite"


class SqliteBackend:
    """Per-session SQLite store. One `runs.sqlite` per session."""

    def __init__(self, path: str | None = None, **_: Any) -> None:
        self._root = (
            Path(path) if path
            else (Path.home() / ".digitorn" / "runs" / "sqlite")
        )
        self._router = PerSessionRouter(self._root)


    async def setup(self) -> None:
        await self._router.setup()

    async def teardown(self) -> None:
        await self._router.teardown()


    async def _ensure_schema(self, db_path: Path) -> None:
        # Idempotent CREATE TABLE IF NOT EXISTS - cheap to call once
        # per session-dir initialisation.
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA busy_timeout=30000")
            await db.executescript(_SCHEMA_SQL)
            await db.commit()

    def _session_db_for_lookup(self, run_id: str) -> Optional[Path]:
        d = self._router.session_dir_for_lookup(run_id)
        return None if d is None else (d / _DB_FILENAME)


    async def start_run(
        self,
        *,
        run_id: str,
        ctx_snapshot: dict[str, Any],
        max_turns: Optional[int],
        parent_run_id: Optional[str],
        task_summary: Optional[str],
        queued_at_iso: str,
        started_at_iso: str,
    ) -> None:
        app_id = ctx_snapshot.get("app_id") or ""
        external_sid = ctx_snapshot.get("session_id") or ""
        session_dir = self._router.session_dir_for_start(run_id, app_id, external_sid)
        db_path = session_dir / _DB_FILENAME
        await self._ensure_schema(db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO agent_runs (
                    id, user_id, app_id, external_sid, agent_id,
                    parent_run_id, status, specialist, provider, model,
                    max_turns, task_summary, queued_at, started_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    run_id,
                    ctx_snapshot.get("user_id") or "",
                    app_id,
                    external_sid,
                    ctx_snapshot.get("agent_id") or "default",
                    parent_run_id,
                    ctx_snapshot.get("agent_id") or "default",
                    ctx_snapshot.get("provider"),
                    ctx_snapshot.get("model"),
                    max_turns,
                    task_summary,
                    queued_at_iso,
                    started_at_iso,
                ),
            )
            await db.commit()

    async def complete_run(
        self,
        *,
        run_id: str,
        status: str,
        prompt_tokens: int,
        completion_tokens: int,
        turns_used: int,
        status_reason: Optional[str],
        completed_at_iso: str,
    ) -> None:
        db_path = self._session_db_for_lookup(run_id)
        if db_path is None:
            return  # unknown run - drop silently (best-effort).
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                UPDATE agent_runs SET
                    status = ?,
                    status_reason = ?,
                    completed_at = ?,
                    last_event_at = ?,
                    prompt_tokens = ?,
                    completion_tokens = ?,
                    turns_used = ?
                WHERE id = ?
                """,
                (
                    status,
                    status_reason,
                    completed_at_iso,
                    completed_at_iso,
                    prompt_tokens,
                    completion_tokens,
                    turns_used,
                    run_id,
                ),
            )
            await db.commit()

    async def emit_event(
        self,
        *,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
        sequence: int,
        emitted_at_iso: str,
    ) -> None:
        db_path = self._session_db_for_lookup(run_id)
        if db_path is None:
            return
        async with aiosqlite.connect(db_path) as db:
            row = await db.execute_fetchall(
                "SELECT started_at FROM agent_runs WHERE id = ?",
                (run_id,),
            )
            elapsed_ms: Optional[int] = None
            if row:
                started_iso = row[0][0]
                if started_iso:
                    try:
                        delta = (
                            datetime.fromisoformat(emitted_at_iso)
                            - datetime.fromisoformat(started_iso)
                        ).total_seconds() * 1000.0
                        elapsed_ms = int(delta)
                    except ValueError:
                        elapsed_ms = None
            await db.execute(
                """
                INSERT OR REPLACE INTO agent_run_events
                    (run_id, sequence, event_type, data, elapsed_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    event_type,
                    json.dumps(data, default=str, ensure_ascii=False),
                    elapsed_ms,
                    emitted_at_iso,
                ),
            )
            await db.execute(
                "UPDATE agent_runs SET last_event_at = ? WHERE id = ?",
                (emitted_at_iso, run_id),
            )
            await db.commit()

    async def increment_turns(self, *, run_id: str) -> None:
        db_path = self._session_db_for_lookup(run_id)
        if db_path is None:
            return
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE agent_runs SET turns_used = turns_used + 1 WHERE id = ?",
                (run_id,),
            )
            await db.commit()

    async def increment_sub_agents(self, *, run_id: str) -> None:
        db_path = self._session_db_for_lookup(run_id)
        if db_path is None:
            return
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE agent_runs "
                "SET sub_agents_spawned = sub_agents_spawned + 1 WHERE id = ?",
                (run_id,),
            )
            await db.commit()
