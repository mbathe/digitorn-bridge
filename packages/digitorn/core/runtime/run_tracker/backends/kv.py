"""KV backend for the local mode (one dbm file per session, stdlib only).

Each session gets its own ``runs.kv`` under
``<root>/<app_id>/<external_sid>/``. Two parallel sessions write to
two separate dbm files - no shared lock, true OS-level parallelism.
``run:<run_id>`` and ``events:<run_id>`` keys live INSIDE one
session's file; cross-session lookups go through the
``PerSessionRouter``.

Why this layout (vs the previous one-big-file approach):

  * Bounded size: a session's file is bounded by its own activity,
    not by the daemon's lifetime history.
  * Real parallelism: dbm holds a per-file fcntl lock; with one
    file per session, every session is independent.
  * Operationally simple: deleting a session's data is one
    ``rm -rf <session_dir>``.

Stdlib only: dbm ships with Python on every platform. On Windows
that means dbm.dumb (slow but works); on Linux usually dbm.gdbm.
For higher throughput, swap to the ``sqlite`` backend - same
per-session layout, faster engine.

Concurrency: the worker is single-consumer per process. Each
backend method opens the dbm file for the call and closes after.
That's a tradeoff: per-call open is ~50us-1ms but it lets a
crashing call leave a recoverable file (no half-flushed writes
hiding behind a long-lived handle).
"""

from __future__ import annotations

import asyncio
import dbm
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from digitorn.core.runtime.run_tracker.backends._perfile import PerSessionRouter

logger = logging.getLogger(__name__)


_DB_FILENAME = "runs.kv"


class KVBackend:
    """Per-session dbm store. One ``runs.kv`` per session."""

    def __init__(self, path: str | None = None, **_: Any) -> None:
        self._root = (
            Path(path) if path
            else (Path.home() / ".digitorn" / "runs" / "kv")
        )
        self._router = PerSessionRouter(self._root)

    # ── lifecycle ────────────────────────────────────────────────

    async def setup(self) -> None:
        await self._router.setup()

    async def teardown(self) -> None:
        await self._router.teardown()

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _run_key(run_id: str) -> bytes:
        return f"run:{run_id}".encode("utf-8")

    @staticmethod
    def _events_key(run_id: str) -> bytes:
        return f"events:{run_id}".encode("utf-8")

    @staticmethod
    def _read(db: Any, key: bytes) -> Optional[dict[str, Any]]:
        try:
            raw = db[key]
        except KeyError:
            return None
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _write(db: Any, key: bytes, value: Any) -> None:
        db[key] = json.dumps(value, default=str, ensure_ascii=False).encode("utf-8")

    def _session_db_for_lookup(self, run_id: str) -> Optional[Path]:
        d = self._router.session_dir_for_lookup(run_id)
        return None if d is None else (d / _DB_FILENAME)

    # ── writes (offloaded to a thread; dbm I/O is sync) ─────────

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

        record = {
            "id": run_id,
            "user_id": ctx_snapshot.get("user_id") or "",
            "app_id": app_id,
            "external_sid": external_sid,
            "agent_id": ctx_snapshot.get("agent_id") or "default",
            "specialist": ctx_snapshot.get("agent_id") or "default",
            "provider": ctx_snapshot.get("provider"),
            "model": ctx_snapshot.get("model"),
            "parent_run_id": parent_run_id,
            "max_turns": max_turns,
            "task_summary": task_summary,
            "status": "active",
            "status_reason": None,
            "turns_used": 0,
            "sub_agents_spawned": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "queued_at": queued_at_iso,
            "started_at": started_at_iso,
            "completed_at": None,
            "last_event_at": None,
        }

        def _do() -> None:
            with dbm.open(str(db_path), "c") as db:
                self._write(db, self._run_key(run_id), record)
                self._write(db, self._events_key(run_id), [])

        await asyncio.to_thread(_do)

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
            return

        def _do() -> None:
            with dbm.open(str(db_path), "c") as db:
                record = self._read(db, self._run_key(run_id)) or {"id": run_id}
                record["status"] = status
                record["status_reason"] = status_reason
                record["completed_at"] = completed_at_iso
                record["last_event_at"] = completed_at_iso
                record["prompt_tokens"] = int(prompt_tokens)
                record["completion_tokens"] = int(completion_tokens)
                record["turns_used"] = int(turns_used)
                self._write(db, self._run_key(run_id), record)

        await asyncio.to_thread(_do)

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

        def _do() -> None:
            with dbm.open(str(db_path), "c") as db:
                run_record = self._read(db, self._run_key(run_id))
                elapsed_ms: Optional[int] = None
                if run_record and run_record.get("started_at"):
                    try:
                        elapsed_ms = int((
                            datetime.fromisoformat(emitted_at_iso)
                            - datetime.fromisoformat(run_record["started_at"])
                        ).total_seconds() * 1000.0)
                    except ValueError:
                        elapsed_ms = None
                events = self._read(db, self._events_key(run_id)) or []
                events.append({
                    "sequence": int(sequence),
                    "event_type": event_type,
                    "data": data,
                    "elapsed_ms": elapsed_ms,
                    "created_at": emitted_at_iso,
                })
                self._write(db, self._events_key(run_id), events)
                if run_record is not None:
                    run_record["last_event_at"] = emitted_at_iso
                    self._write(db, self._run_key(run_id), run_record)

        await asyncio.to_thread(_do)

    async def increment_turns(self, *, run_id: str) -> None:
        db_path = self._session_db_for_lookup(run_id)
        if db_path is None:
            return

        def _do() -> None:
            with dbm.open(str(db_path), "c") as db:
                record = self._read(db, self._run_key(run_id))
                if record is None:
                    return
                record["turns_used"] = int(record.get("turns_used") or 0) + 1
                self._write(db, self._run_key(run_id), record)

        await asyncio.to_thread(_do)

    async def increment_sub_agents(self, *, run_id: str) -> None:
        db_path = self._session_db_for_lookup(run_id)
        if db_path is None:
            return

        def _do() -> None:
            with dbm.open(str(db_path), "c") as db:
                record = self._read(db, self._run_key(run_id))
                if record is None:
                    return
                record["sub_agents_spawned"] = int(record.get("sub_agents_spawned") or 0) + 1
                self._write(db, self._run_key(run_id), record)

        await asyncio.to_thread(_do)
