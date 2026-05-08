"""Atomic read/write of per-session meta.json.

Holds {last_seq, event_count, started_at, ended_at, ...}: the metadata
the SeqAllocator's seed_loader needs in O(1) on cold start, plus the
SessionIndex summary on close.

Writes are atomic via tmp file + os.replace. Reads return None on
missing/corrupt files so the caller can treat this as "no session
yet" without try/except dance.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MetaIO:
    """Atomic meta.json read/write per session directory."""

    @staticmethod
    def read(session_dir: Path) -> dict[str, Any] | None:
        path = session_dir / "meta.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("meta_read_corrupt path=%s err=%s", path, exc)
            return None

    @staticmethod
    def write(session_dir: Path, meta: dict[str, Any]) -> None:
        """Atomic write via tmp + replace. Survives mid-write crashes."""
        session_dir.mkdir(parents=True, exist_ok=True)
        target = session_dir / "meta.json"
        fd, tmp_path = tempfile.mkstemp(
            prefix=".meta_", suffix=".tmp", dir=str(session_dir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, default=str, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def update(session_dir: Path, **fields: Any) -> dict[str, Any]:
        """Read-modify-write convenience. Caller is responsible for
        serialising concurrent updates per session (the DiskFlusher's
        single-writer-per-session contract handles this in prod)."""
        meta = MetaIO.read(session_dir) or {}
        meta.update(fields)
        MetaIO.write(session_dir, meta)
        return meta

    @staticmethod
    def last_seq_from_jsonl_tail(session_dir: Path) -> int:
        """Fallback for the case where meta.json is missing or
        corrupt: read the last line of events.jsonl and parse its
        ``seq``. Slower than reading meta.json (full file open + seek
        to end) but bulletproof."""
        path = session_dir / "events.jsonl"
        if not path.exists():
            return 0
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size == 0:
                    return 0
                chunk = 4096
                pos = max(0, size - chunk)
                f.seek(pos)
                tail = f.read(size - pos).decode("utf-8", errors="replace")
            lines = [ln for ln in tail.split("\n") if ln.strip()]
            if not lines:
                return 0
            last = json.loads(lines[-1])
            return int(last.get("seq", 0))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "jsonl_tail_read_failed path=%s err=%s defaulting to 0",
                path, exc,
            )
            return 0
