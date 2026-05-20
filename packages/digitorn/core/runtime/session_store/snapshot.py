"""Snapshot: render-ready JSON state of a session."""

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
    """Reduce a SessionState's projections into a render-ready dict."""
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
        "goal": state.goal,
        "semantic_facts": list(state.semantic_facts),
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
        # Chat-level metadata absorbed from ConversationSession.
        "title": state.title,
        "turn_count": state.turn_count,
        "workspace": state.workspace,
        "workdir": state.workdir,
        "interrupted": state.interrupted,
        "interrupted_at": state.interrupted_at,
        "active_mode_id": state.active_mode_id,
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
    """Read snapshot.json"""
    path = session_dir / SNAPSHOT_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("snapshot_read_corrupt path=%s err=%s", path, exc)
        return None
