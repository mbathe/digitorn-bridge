"""JSONL backend for the local mode (one file per session)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from digitorn.core.runtime.run_tracker.backends._perfile import PerSessionRouter

logger = logging.getLogger(__name__)


_LOG_FILENAME = "runs.jsonl"


class JsonFileBackend:
    """Per-session append-only JSONL log."""

    def __init__(self, path: str | None = None, **_: Any) -> None:
        self._root = (
            Path(path) if path
            else (Path.home() / ".digitorn" / "runs" / "jsonl")
        )
        self._router = PerSessionRouter(self._root)


    async def setup(self) -> None:
        await self._router.setup()

    async def teardown(self) -> None:
        await self._router.teardown()


    def _file_for_lookup(self, run_id: str) -> Optional[Path]:
        d = self._router.session_dir_for_lookup(run_id)
        return None if d is None else (d / _LOG_FILENAME)

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Per-line open/close keeps the file durable and avoids holding
        # handles for sessions that are no longer hot.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass


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
        path = session_dir / _LOG_FILENAME
        record = {
            "kind": "start",
            "run_id": run_id,
            "ctx": ctx_snapshot,
            "max_turns": max_turns,
            "parent_run_id": parent_run_id,
            "task_summary": task_summary,
            "queued_at": queued_at_iso,
            "started_at": started_at_iso,
        }
        await asyncio.to_thread(self._append, path, record)

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
        path = self._file_for_lookup(run_id)
        if path is None:
            return
        record = {
            "kind": "complete",
            "run_id": run_id,
            "status": status,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "turns_used": turns_used,
            "status_reason": status_reason,
            "completed_at": completed_at_iso,
        }
        await asyncio.to_thread(self._append, path, record)

    async def emit_event(
        self,
        *,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
        sequence: int,
        emitted_at_iso: str,
    ) -> None:
        path = self._file_for_lookup(run_id)
        if path is None:
            return
        record = {
            "kind": "event",
            "run_id": run_id,
            "sequence": sequence,
            "event_type": event_type,
            "data": data,
            "emitted_at": emitted_at_iso,
        }
        await asyncio.to_thread(self._append, path, record)

    async def increment_turns(self, *, run_id: str) -> None:
        path = self._file_for_lookup(run_id)
        if path is None:
            return
        ts = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(
            self._append, path,
            {"kind": "inc_turns", "run_id": run_id, "ts": ts},
        )

    async def increment_sub_agents(self, *, run_id: str) -> None:
        path = self._file_for_lookup(run_id)
        if path is None:
            return
        ts = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(
            self._append, path,
            {"kind": "inc_subs", "run_id": run_id, "ts": ts},
        )
