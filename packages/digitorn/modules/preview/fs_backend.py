"""Filesystem persistence for preview state - the single source of truth.

Every session has a directory under
``{workspace}/.digitorn/sessions/{session_id}/`` that holds the
canonical snapshot of its preview state. Copying the workspace dir
exports the session wholesale (cross-machine migration, git-trackable).

Atomic writes: state is serialised to ``state.json.tmp`` then renamed
to ``state.json``. On POSIX rename is atomic; on Windows os.replace is
atomic since 3.3.

Layout:
    {workspace}/.digitorn/sessions/{sid}/
        state.json          - {app_id, user_id, state, resources, saved_at}
        baselines/<path>    - last-approved version of a workspace file
        baselines/<path>.history/_index.json
                            - revision log for that file
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
    saved_at: str,
) -> None:
    """Atomically write the snapshot to ``state.json`` under the session dir.

    No ``seq`` field - preview events derive their ordering key from
    the ``SessionBus`` envelope seq stored in ``history_log``, not
    from a per-state-file counter. The state.json is just "current
    state at flush time", not a timestamped log.
    """
    sd = session_dir(workspace, session_id)
    payload = {
        "format": "digitorn.workspace.snapshot",
        "version": 2,
        "app_id": app_id,
        "user_id": user_id,
        "session_id": session_id,
        "state": state,
        "resources": resources,
        "saved_at": saved_at,
    }
    tmp = sd / "state.json.tmp"
    final = sd / "state.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)


_KNOWN_VERSIONS: frozenset[int] = frozenset({1, 2})


def read_snapshot(workspace: str, session_id: str) -> dict[str, Any] | None:
    """Return the persisted snapshot dict, or None if missing / unreadable.

    Forward-tolerant: a v1 file (carries the deprecated ``seq`` field)
    is read as-is and the caller's ``restore_from_dict`` ignores
    fields it does not recognise. Files with an unknown ``version`` are
    still returned but log a warning so a stale daemon notices it is
    reading data it does not fully understand.
    """
    try:
        sd = Path(workspace).expanduser() / ".digitorn" / "sessions" / session_id
        f = sd / "state.json"
        if not f.is_file():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning(
                "fs_backend_read_invalid_shape sid=%s type=%s",
                session_id, type(data).__name__,
            )
            return None
        version = int(data.get("version") or 0)
        if version and version not in _KNOWN_VERSIONS:
            logger.warning(
                "fs_backend_read_unknown_version sid=%s version=%d "
                "(known: %s) - reading optimistically, fields beyond "
                "state/resources/user_id will be ignored",
                session_id, version, sorted(_KNOWN_VERSIONS),
            )
        return data
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
    record_in_history: bool = True,
) -> None:
    """Store the baseline (last-approved) version of a workspace file.

    Also appends a revision entry to ``{baseline}.history/_index.json`` -
    lets the client answer "when was this file last approved?" / "how
    many revisions since session start?". Revision bodies land beside
    the index for diff-between-revisions support.

    Pass ``record_in_history=False`` for synthetic baselines (e.g.
    auto-snapshot on first agent touch) - they still pin the baseline
    so future diffs work, but they don't pollute the user-visible
    history view with implementation-detail entries.
    """
    import time as _time
    p = baseline_path(workspace, session_id, rel_path)
    p.write_text(content, encoding="utf-8")

    if not record_in_history:
        return

    try:
        hist_dir = p.with_name(p.name + ".history")
        hist_dir.mkdir(parents=True, exist_ok=True)
        idx_path = hist_dir / "_index.json"
        revisions = _read_history_index(idx_path)
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


def _read_history_index(idx_path: Path) -> list[dict[str, Any]]:
    """Read a baseline history ``_index.json``, recovering from corruption.

    Silent-reset on parse failure caused real users to lose their entire
    edit history (single-character disk corruption wiped 100 revisions).
    Now we back the malformed file up to ``_index.json.corrupt-<ts>``
    and log a WARNING so the caller is aware data was salvaged out of
    the active path.
    """
    if not idx_path.is_file():
        return []
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        import time as _time
        backup = idx_path.with_suffix(f".json.corrupt-{int(_time.time())}")
        try:
            os.replace(idx_path, backup)
        except Exception:
            pass
        logger.warning(
            "baseline_history_index_corrupted path=%s backup=%s err=%s - "
            "starting fresh history; previous file preserved at backup",
            idx_path, backup, exc,
        )
        return []


def has_user_approval(
    workspace: str, session_id: str, rel_path: str,
) -> bool:
    """Return True iff the baseline was set at least once by an explicit
    user/auto approval (not just our internal session-start snapshot).

    Lets ``reject_file`` distinguish "the user actually approved this"
    (in which case reject = revert to baseline) from "we just auto-
    snapshotted on first touch and the user has never approved"
    (in which case reject = delete the file entirely, which matches
    the user's mental model: a brand-new file they didn't ratify
    should not survive a reject).
    """
    try:
        hist_dir = baseline_path(workspace, session_id, rel_path).with_name(
            baseline_path(workspace, session_id, rel_path).name + ".history",
        )
        idx = hist_dir / "_index.json"
        if not idx.is_file():
            return False
        for r in _read_history_index(idx):
            if r.get("approved_by") not in ("session-start", None):
                return True
        return False
    except Exception:
        return False


def read_baseline(workspace: str, session_id: str, rel_path: str) -> str | None:
    """Return the baseline content of a file, or None if no baseline yet.

    Defends against silent disk corruption: if ``_index.json`` records
    a known byte length for the latest revision and the on-disk file
    no longer matches, return ``None`` rather than the truncated
    content. Otherwise diff comparisons against a half-written
    baseline produce nonsense pending counters and a future approve
    cements the corruption.
    """
    try:
        base = Path(workspace).expanduser() / ".digitorn" / "sessions" / session_id / "baselines"
        p = base / rel_path.lstrip("/")
        if not p.is_file():
            return None
        content = p.read_text(encoding="utf-8")
        # Cross-check against the history index's recorded byte length.
        try:
            idx_path = p.with_name(p.name + ".history") / "_index.json"
            if idx_path.is_file():
                revs = _read_history_index(idx_path)
                if revs:
                    expected = revs[-1].get("bytes")
                    if isinstance(expected, int) and expected > 0:
                        actual = len(content.encode("utf-8"))
                        if actual != expected:
                            logger.warning(
                                "baseline_size_mismatch path=%s expected=%d "
                                "actual=%d - treating as missing baseline",
                                rel_path, expected, actual,
                            )
                            return None
        except Exception:
            # Best-effort check; never block the read on a stat failure.
            pass
        return content
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
