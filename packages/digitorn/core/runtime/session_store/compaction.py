"""Compaction record: cursor saying ``RAM holds events from seq X
onward + this summary``.

Disk file: ``compaction.json``. ONE record per session. Each compaction
overwrites the previous one (history of past compactions lives in
``events.jsonl`` as ``compact_done`` rows).

events.jsonl is NEVER touched by compaction. The disk journal stays
the full chronology -- frontend ``stream_full_history`` reads it as-is
and the user sees every event.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


COMPACTION_FILENAME = "compaction.json"


@dataclass
class Compaction:
    """The latest compaction record for a session.

    Fields:
      cutoff_seq        -- events with seq <= cutoff_seq are NOT in
                           memory; they live only on events.jsonl.
      summary           -- caller-generated text. Injected as a system
                           message at index 0 on load.
      strategy          -- "summary" | "summary_plus_keys" | "truncate".
      key_events        -- if strategy is summary_plus_keys, verbatim
                           events the caller decided to preserve in
                           memory. Stored as serialized event dicts.
      created_at        -- ISO timestamp of the compaction.
      tokens_estimate   -- REAL token count from litellm
                           (no len/4 heuristic).
      model             -- model id used for the count.
    """

    cutoff_seq: int
    summary: str
    strategy: str
    key_events: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    tokens_estimate: int = 0
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Compaction":
        return cls(
            cutoff_seq=int(d["cutoff_seq"]),
            summary=str(d.get("summary", "")),
            strategy=str(d.get("strategy", "summary")),
            key_events=list(d.get("key_events") or []),
            created_at=str(d.get("created_at", "")),
            tokens_estimate=int(d.get("tokens_estimate", 0)),
            model=str(d.get("model", "")),
        )


def read_compaction(session_dir: Path) -> Compaction | None:
    """Read compaction.json. Returns None if missing or corrupt -- the
    caller can treat that as ``no compaction yet``."""
    path = session_dir / COMPACTION_FILENAME
    if not path.exists():
        return None
    try:
        return Compaction.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, KeyError, TypeError) as exc:
        logger.warning(
            "compaction_read_corrupt path=%s err=%s -- treating as no compaction",
            path, exc,
        )
        return None


def write_compaction(session_dir: Path, comp: Compaction) -> None:
    """Atomic overwrite via tmp + os.replace."""
    session_dir.mkdir(parents=True, exist_ok=True)
    target = session_dir / COMPACTION_FILENAME
    fd, tmp = tempfile.mkstemp(
        prefix=".comp_", suffix=".tmp", dir=str(session_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(comp.to_dict(), f, default=str, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
