"""Snapshot: render-ready JSON state of a session.

The snapshot is what the frontend renders for instant reopen. Built on
``close_session`` (or on demand) by reducing ``state.events`` into the
final UI projections. Stored as ``snapshot.json`` next to
``events.jsonl`` and ``meta.json``.

Read path:
  ``GET /sessions/{sid}/snapshot``
    → ``InMemorySessionStore.read_snapshot(sid)``
    → 5 ms cold (one open + json.load), <100 µs warm (page cache).

Write path:
  ``store.close_session(sid)``
    → ``build_snapshot(state)`` reduces events into UI dicts
    → ``write_snapshot(session_dir, snap)`` writes atomically.

Snapshot is intentionally smaller than the events journal: it carries
the FINAL projected state (assistant_message text, not every token
delta). Full replay is still available via ``stream_events``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from digitorn.core.runtime.session_store.session_state import SessionState

logger = logging.getLogger(__name__)


SNAPSHOT_FILENAME = "snapshot.json"


def build_snapshot(state: SessionState) -> dict[str, Any]:
    """Reduce a SessionState's projections into a render-ready dict.

    Streaming chunks (token, thinking_delta, ...) are NOT in the
    snapshot -- the assembled assistant_message in ``state.messages``
    captures their result. The events journal still has every chunk
    for full replay.
    """
    return {
        "session_id": state.session_id,
        "app_id": state.app_id,
        "user_id": state.user_id,
        "parent_link": (
            state.parent_link.to_dict() if state.parent_link is not None
            else None
        ),
        "first_seq": state.first_seq,
        "last_seq": state.last_seq,
        "event_count": len(state.events),
        "started_at": state.started_at,
        "ended_at": state.ended_at,
        "closed": state.closed,
        "messages": [m.to_dict() for m in state.messages],
        "tool_calls": {k: v.to_dict() for k, v in state.tool_calls.items()},
        "tool_results": {k: v.to_dict() for k, v in state.tool_results.items()},
        "todos": [t.to_dict() for t in state.todos],
        "memory_facts": dict(state.memory_facts),
        "workspace_files": {
            k: v.to_dict() for k, v in state.workspace_files.items()
        },
        "children": [c.to_dict() for c in state.children],
        "pending_approvals": {
            k: v.to_dict() for k, v in state.pending_approvals.items()
        },
        "blobs": {k: v.to_dict() for k, v in state.blobs.items()},
        "cost_total": state.cost_total,
        "tokens_in": state.tokens_in,
        "tokens_out": state.tokens_out,
    }


def write_snapshot(session_dir: Path, snapshot: dict[str, Any]) -> None:
    """Atomic write via tmp + replace."""
    session_dir.mkdir(parents=True, exist_ok=True)
    target = session_dir / SNAPSHOT_FILENAME
    fd, tmp = tempfile.mkstemp(
        prefix=".snap_", suffix=".tmp", dir=str(session_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, default=str, ensure_ascii=False)
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


def read_snapshot(session_dir: Path) -> dict[str, Any] | None:
    """Read snapshot.json. Returns None if missing or corrupt; the
    caller can fall back to rebuilding from events.jsonl."""
    path = session_dir / SNAPSHOT_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("snapshot_read_corrupt path=%s err=%s", path, exc)
        return None
