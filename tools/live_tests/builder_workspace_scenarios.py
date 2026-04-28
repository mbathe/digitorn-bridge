"""Builder-app workspace + preview live scenarios.

Pin down three reported symptoms against digitorn-builder:

  1. The preview does not render on rejoin (no ``preview:snapshot``
     or empty payload).
  2. The per-session workspace dir is not created on disk under
     ``~/.digitorn/workspaces/``.
  3. The state store (``session_workspace_snapshots``) is not synced
     in real time during a turn.

We send a prompt that forces a workspace write, then inspect disk,
DB and Socket.IO in one sweep.
"""
from __future__ import annotations

import json
import os
import os as _os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from digitorn.testing.client import DevClient
from digitorn.testing.models import SessionHandle


_APP_ID = _os.environ.get("TEST_APP_ID", "ws-preview-test")
_PROMPT = (
    "Crée un seul fichier hello.txt contenant la ligne 'Bonjour' "
    "et rien d'autre. N'explique rien, juste le tool call."
)


def _walk_workspace_root() -> dict[str, list[str]]:
    """Snapshot of every dir directly under ~/.digitorn/workspaces/ and
    every dir under any <app_id>/ subfolder."""
    root = Path.home() / ".digitorn" / "workspaces"
    if not root.exists():
        return {"root": [], "app_scoped": []}
    top = [p.name for p in root.iterdir() if p.is_dir()]
    nested: list[str] = []
    for p in root.iterdir():
        if not p.is_dir():
            continue
        # If this is an app-scoped bucket, inner dirs are session ids.
        for inner in p.iterdir() if p.is_dir() else []:
            if inner.is_dir():
                nested.append(f"{p.name}/{inner.name}")
    return {"root": top, "app_scoped": nested}


def _dir_listing(path: Path, max_entries: int = 20) -> list[str]:
    if not path.exists() or not path.is_dir():
        return []
    out: list[str] = []
    for p in sorted(path.iterdir()):
        out.append(p.name + ("/" if p.is_dir() else ""))
        if len(out) >= max_entries:
            break
    return out


def _read_snapshot_row(session_id: str) -> dict[str, Any] | None:
    """Read the ``session_workspace_snapshots`` DB row for a session."""
    candidates = [
        Path.cwd() / "digitorn.db",
        Path.home() / ".digitorn" / "digitorn.db",
    ]
    db_path = next((p for p in candidates if p.exists()), None)
    if db_path is None:
        return None
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(
            "SELECT session_id, app_id, user_id, state, resources, "
            "preview_seq, snapshot_version, saved_at "
            "FROM session_workspace_snapshots WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        out: dict[str, Any] = dict(row)
        for k in ("state", "resources"):
            v = out.get(k)
            if isinstance(v, str):
                try:
                    out[k] = json.loads(v)
                except Exception:
                    pass
        return out
    except sqlite3.OperationalError as exc:
        return {"__error": str(exc)}
    finally:
        con.close()


def _find_workspace_dir(session_id: str) -> dict[str, Any]:
    """Locate where on disk the session's workspace ended up."""
    root = Path.home() / ".digitorn" / "workspaces"
    flat = root / session_id
    found: list[str] = []
    if flat.exists() and flat.is_dir():
        found.append(str(flat))
    # app-scoped form: ~/.digitorn/workspaces/<app_id>/<sid>
    for sub in root.iterdir() if root.exists() else []:
        if not sub.is_dir():
            continue
        nested = sub / session_id
        if nested.exists() and nested.is_dir():
            found.append(str(nested))
    return {
        "flat_expected": str(flat),
        "found_paths": found,
        "flat_listing": _dir_listing(flat) if flat.exists() else [],
        "nested_listings": {
            p: _dir_listing(Path(p)) for p in found if Path(p) != flat
        },
    }


def run(daemon_url: str, email: str, password: str) -> int:
    client = DevClient.with_user(
        email, password, daemon_url=daemon_url, register_if_missing=True,
    )

    # Baseline snapshot of the workspaces dir before we do anything.
    baseline = _walk_workspace_root()

    sid = f"build-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=_APP_ID, daemon_url=daemon_url, workspace="",
    )

    print(f"\n=== SCENARIO builder_workspace ===")
    print(f"session_id = {sid}")
    print(f"app_id     = {_APP_ID}")

    # POST first - this creates the session in the manager. Then open
    # the stream with since=0 so we replay every event including any
    # preview:* emitted before the socket joined.
    post = client.post_message_raw(session, _PROMPT)
    cid = (post.get("body") or {}).get("data", {}).get("correlation_id") or ""
    print(f"correlation_id = {cid}")

    stream = client.open_event_stream(session, wait_for_session=True)
    try:
        done = stream.wait_for(
            "message_done",
            timeout=180.0,
            predicate=lambda e: (
                (e.get("payload") or {}).get("correlation_id") == cid
            ),
        )
        time.sleep(1.0)  # flush trailing snapshot debounce

        events = stream.events()
    finally:
        stream.stop(timeout=2.0)

    # ── Collect observations ──────────────────────────────────────
    by_type: dict[str, int] = {}
    preview_snapshot_events: list[dict[str, Any]] = []
    preview_resource_events: list[dict[str, Any]] = []
    for env in events:
        t = str(env.get("type") or "")
        by_type[t] = by_type.get(t, 0) + 1
        if t == "preview:snapshot":
            preview_snapshot_events.append(env)
        if t.startswith("preview:resource"):
            preview_resource_events.append(env)

    workspace_dir = _find_workspace_dir(sid)
    snapshot_row = _read_snapshot_row(sid)
    after = _walk_workspace_root()

    # New dirs that appeared during the scenario.
    new_top = sorted(set(after["root"]) - set(baseline["root"]))
    new_nested = sorted(set(after["app_scoped"]) - set(baseline["app_scoped"]))

    # Also check /history.events + turn_active for the new endpoint
    # behaviour.
    hist = client._get(
        f"/api/apps/{_APP_ID}/sessions/{sid}/history",
        params={"include_system": "true"},
    ).json().get("data", {}) or {}

    # ── Checks ────────────────────────────────────────────────────
    checks: list[tuple[str, bool, str]] = []

    checks.append((
        "message_done received",
        done is not None,
        f"correlation_id={cid}",
    ))

    checks.append((
        "at least one preview:resource_set emitted during turn",
        any(e.get("type") == "preview:resource_set" for e in events),
        f"preview_resource_events={len(preview_resource_events)}",
    ))

    # Symptom 1 - preview rendering. We re-open a fresh stream (simulating
    # a client rejoin) and check whether a preview:snapshot is received.
    rejoin = client.open_event_stream(session, wait_for_session=False)
    try:
        rejoin_snap = rejoin.wait_for(
            "preview:snapshot", timeout=10.0,
        )
        rejoin_events_seen = [
            (e.get("type"), (e.get("payload") or {}).get("session_id"))
            for e in rejoin.events()
        ]
    finally:
        rejoin.stop(timeout=2.0)

    checks.append((
        "REJOIN emits preview:snapshot",
        rejoin_snap is not None,
        f"received={rejoin_snap is not None}",
    ))
    if rejoin_snap is not None:
        payload = rejoin_snap.get("payload") or {}
        files_ch = (payload.get("resources") or {}).get("files") or {}
        checks.append((
            "REJOIN preview:snapshot carries 'files' channel",
            bool(files_ch),
            f"files_keys={list(files_ch.keys())[:10]}",
        ))

    # Symptom 2 - workspace dir on disk.
    checks.append((
        "workspace dir for this session exists on disk",
        bool(workspace_dir["found_paths"]),
        f"paths={workspace_dir['found_paths']}",
    ))

    # Symptom 3 - state store synced. Builder-style apps (sync_to_disk
    # true) persist through the filesystem backend at
    # ``{workspace}/.digitorn/sessions/<sid>/state.json``; pure-memory
    # apps fall back to the ``session_workspace_snapshots`` DB row.
    # Accept either.
    fs_state_path = None
    fs_state_data: dict[str, Any] | None = None
    for p in workspace_dir.get("found_paths") or []:
        candidate = Path(p) / ".digitorn" / "sessions" / sid / "state.json"
        if candidate.exists():
            fs_state_path = str(candidate)
            try:
                fs_state_data = json.loads(candidate.read_text("utf-8"))
            except Exception:
                fs_state_data = None
            break

    state_synced = (
        (fs_state_data is not None and bool(
            (fs_state_data.get("resources") or {}).get("files")
        ))
        or (
            isinstance(snapshot_row, dict)
            and not snapshot_row.get("__error")
            and snapshot_row.get("session_id") == sid
            and bool((snapshot_row.get("resources") or {}).get("files"))
        )
    )
    checks.append((
        "state store synced (fs state.json OR DB row)",
        state_synced,
        f"fs_state={fs_state_path} db_row={bool(snapshot_row)}",
    ))
    if fs_state_data is not None:
        fs_files = (fs_state_data.get("resources") or {}).get("files") or {}
        checks.append((
            "fs state.json contains the written file",
            "hello.txt" in fs_files,
            f"file_keys={list(fs_files.keys())[:10]}",
        ))

    # Misc diagnostic info - keep in artifacts.
    artifacts = {
        "session_id": sid,
        "event_counts_by_type": dict(sorted(by_type.items())),
        "preview_snapshot_count": len(preview_snapshot_events),
        "workspace_dir": workspace_dir,
        "new_top_dirs_during_scenario": new_top,
        "new_nested_dirs_during_scenario": new_nested,
        "snapshot_row_keys": list(snapshot_row.keys()) if isinstance(snapshot_row, dict) else None,
        "rejoin_events_seen": rejoin_events_seen,
        "history_turn_active": hist.get("turn_active"),
        "history_pending_queue_len": len(hist.get("pending_queue") or []),
        "history_messages_count": len(hist.get("messages") or []),
        "history_events_count": len(hist.get("events") or []),
    }

    # ── Output ────────────────────────────────────────────────────
    print()
    passed = 0
    for name, ok, detail in checks:
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {name}: {detail}")
        if ok:
            passed += 1
    total = len(checks)
    print(f"\n{passed}/{total} checks passed")
    print("\n--- artifacts ---")
    print(json.dumps(artifacts, indent=2, default=str, ensure_ascii=False))

    return 0 if passed == total else 1


if __name__ == "__main__":
    daemon_url = _os.environ.get("DAEMON_URL", "http://127.0.0.1:8000")
    email = _os.environ.get("DEV_EMAIL", "dev@digitorn.local")
    password = _os.environ.get("DEV_PASSWORD", "DevPassword123!")
    sys.exit(run(daemon_url, email, password))
