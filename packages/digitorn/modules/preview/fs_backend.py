"""Filesystem persistence backend for preview snapshots.

Selected when the session's workspace dir is user-chosen (i.e. the user
picked a project folder — Lovable-style). In that case state lives under
``{workspace}/.digitorn/sessions/{session_id}/`` so exporting the folder
exports the session wholesale (cross-machine migration, git-trackable).

Atomic writes: state is serialised to ``state.json.tmp`` then renamed to
``state.json``. On POSIX rename is atomic; on Windows os.replace is
atomic since 3.3.

Layout:
    {workspace}/.digitorn/sessions/{sid}/
        state.json          — {app_id, user_id, state, resources, seq, saved_at}
        baselines/<path>    — last-approved version of a workspace file
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def session_dir(workspace: str, session_id: str) -> Path:
    """Return ``{workspace}/.digitorn/sessions/{sid}/``, creating parents."""
    p = Path(workspace).expanduser() / ".digitorn" / "sessions" / session_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def baseline_path(workspace: str, session_id: str, rel_path: str) -> Path:
    """Path of the baseline (last-approved version) of a workspace file."""
    base = session_dir(workspace, session_id) / "baselines"
    base.mkdir(parents=True, exist_ok=True)
    target = base / rel_path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def write_snapshot(
    workspace: str,
    session_id: str,
    *,
    app_id: str,
    user_id: str,
    state: dict[str, Any],
    resources: dict[str, dict[str, Any]],
    seq: int,
    saved_at: str,
) -> None:
    """Atomically write the snapshot to ``state.json`` under the session dir."""
    sd = session_dir(workspace, session_id)
    payload = {
        "format": "digitorn.workspace.snapshot",
        "version": 1,
        "app_id": app_id,
        "user_id": user_id,
        "session_id": session_id,
        "state": state,
        "resources": resources,
        "seq": seq,
        "saved_at": saved_at,
    }
    tmp = sd / "state.json.tmp"
    final = sd / "state.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)


def read_snapshot(workspace: str, session_id: str) -> dict[str, Any] | None:
    """Return the persisted snapshot dict, or None if missing / unreadable."""
    try:
        sd = Path(workspace).expanduser() / ".digitorn" / "sessions" / session_id
        f = sd / "state.json"
        if not f.is_file():
            return None
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("fs_backend_read_failed sid=%s: %s", session_id, exc)
        return None


def write_baseline(
    workspace: str,
    session_id: str,
    rel_path: str,
    content: str,
    *,
    approved_by: str = "user",
    insertions: int = 0,
    deletions: int = 0,
) -> None:
    """Store the baseline (last-approved) version of a workspace file.

    Also appends a revision entry to ``{baseline}.history/_index.json`` —
    lets the client answer "when was this file last approved?" / "how
    many revisions since session start?". Revision bodies land beside
    the index for diff-between-revisions support.
    """
    import time as _time
    p = baseline_path(workspace, session_id, rel_path)
    p.write_text(content, encoding="utf-8")

    try:
        hist_dir = p.with_name(p.name + ".history")
        hist_dir.mkdir(parents=True, exist_ok=True)
        idx_path = hist_dir / "_index.json"
        if idx_path.is_file():
            try:
                revisions = json.loads(idx_path.read_text(encoding="utf-8")) or []
            except Exception:
                revisions = []
        else:
            revisions = []
        rev_num = len(revisions) + 1
        rev_file = hist_dir / f"rev-{rev_num:04d}"
        rev_file.write_text(content, encoding="utf-8")
        revisions.append({
            "revision": rev_num,
            "approved_at": _time.time(),
            "approved_by": approved_by,
            "tokens_delta_ins": int(insertions),
            "tokens_delta_del": int(deletions),
            "bytes": len(content.encode("utf-8")),
        })
        tmp_idx = idx_path.with_suffix(".json.tmp")
        tmp_idx.write_text(json.dumps(revisions, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_idx, idx_path)
    except Exception as exc:
        logger.debug("baseline_history_write_failed path=%s: %s", rel_path, exc)


def read_baseline(workspace: str, session_id: str, rel_path: str) -> str | None:
    """Return the baseline content of a file, or None if no baseline yet."""
    try:
        p = Path(workspace).expanduser() / ".digitorn" / "sessions" / session_id / "baselines" / rel_path.lstrip("/")
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


def delete_baseline(workspace: str, session_id: str, rel_path: str) -> None:
    """Remove a file's baseline (used on rejection when the file was added)."""
    try:
        p = Path(workspace).expanduser() / ".digitorn" / "sessions" / session_id / "baselines" / rel_path.lstrip("/")
        if p.is_file():
            p.unlink()
    except Exception:
        pass
