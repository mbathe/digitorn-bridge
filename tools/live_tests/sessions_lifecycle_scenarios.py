"""Live E2E regression test for session lifecycle (create / list / fork / delete).

Targets `digitorn-chat` (light, fast LLM). Real round-trip:
  1. Materialize a session via a real send_live (gpt-5-mini reply).
  2. list_sessions(app_id) shows the new session.
  3. fork_session() creates a new session with same history root.
  4. fork appears in list_sessions.
  5. delete_session(original) returns True.
  6. list_sessions no longer shows the original.

Run:
    py -3.12 tools/live_tests/sessions_lifecycle_scenarios.py
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path("C:/Users/ASUS/Documents/digitorn-bridge/packages")))

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


APP_ID = "digitorn-chat"


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def _session_ids_in(sessions: list[dict[str, Any]]) -> set[str]:
    return {
        s.get("session_id")
        for s in (sessions or [])
        if isinstance(s, dict) and s.get("session_id")
    }


def scenario_session_lifecycle(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    artifacts: dict[str, Any] = {}

    # --- Materialize a real session via send_live + real LLM ---
    sid = f"ls-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts["original_session_id"] = sid

    stream = None
    try:
        stream = client.send_live(session, "Reply with the single word OK.", total_timeout=90)
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    # --- list_sessions should show the new session ---
    list_after_create = client.list_sessions(APP_ID) or []
    ids_after_create = _session_ids_in(list_after_create)
    artifacts["sessions_after_create_count"] = len(ids_after_create)
    artifacts["original_in_list_after_create"] = (sid in ids_after_create)

    # --- fork the session ---
    fork_data = client.fork_session(session) or {}
    forked_sid = (fork_data.get("session_id") or "") if isinstance(fork_data, dict) else ""
    artifacts["forked_session_id"] = forked_sid
    artifacts["fork_response_keys"] = sorted(fork_data.keys()) if isinstance(fork_data, dict) else None

    fork_get_status = None
    fork_get_data = {}
    if forked_sid:
        try:
            r = client._get(f"/api/apps/{APP_ID}/sessions/{forked_sid}")
            fork_get_status = r.status_code
            if fork_get_status == 200:
                fork_get_data = r.json().get("data", {}) or {}
        except Exception as exc:
            fork_get_status = f"{type(exc).__name__}: {exc}"
    artifacts["fork_get_status"] = fork_get_status
    artifacts["fork_get_title"] = fork_get_data.get("title")
    artifacts["fork_get_forked_from"] = (
        fork_get_data.get("forked_from") or fork_get_data.get("source_session_id")
    )

    list_after_fork = client.list_sessions(APP_ID) or []
    ids_after_fork = _session_ids_in(list_after_fork)
    artifacts["fork_in_list_after_fork"] = (
        bool(forked_sid) and forked_sid in ids_after_fork
    )

    # --- delete the original session ---
    delete_ok = client.delete_session(session)
    artifacts["delete_returned"] = delete_ok

    # Give the daemon a moment to update the index, then list again.
    time.sleep(1.0)
    list_after_delete = client.list_sessions(APP_ID) or []
    ids_after_delete = _session_ids_in(list_after_delete)
    artifacts["original_in_list_after_delete"] = (sid in ids_after_delete)

    # The fork should still be directly accessible after deleting source.
    fork_get_status_after_delete = None
    if forked_sid:
        try:
            r = client._get(f"/api/apps/{APP_ID}/sessions/{forked_sid}")
            fork_get_status_after_delete = r.status_code
        except Exception as exc:
            fork_get_status_after_delete = f"{type(exc).__name__}: {exc}"
    artifacts["fork_get_status_after_delete"] = fork_get_status_after_delete

    # Cleanup the fork.
    if forked_sid:
        fork_handle = SessionHandle(
            session_id=forked_sid, app_id=APP_ID,
            daemon_url=client.daemon_url, workspace="",
        )
        try:
            client.delete_session(fork_handle)
        except Exception:
            pass

    checks = [
        _ok(
            "original session appears in list after send_live",
            sid in ids_after_create,
            f"{sid!r} not in {sorted(ids_after_create)[:8]!r}...",
        ),
        _ok(
            "fork returns a non-empty session_id (UUID)",
            bool(forked_sid) and forked_sid != sid,
            f"fork data: {fork_data!r}",
        ),
        _ok(
            "forked session accessible via direct GET",
            fork_get_status == 200,
            f"GET /sessions/{forked_sid} -> {fork_get_status}",
        ),
        _ok(
            "GET /sessions/{fork} exposes forked_from == source session_id",
            artifacts.get("fork_get_forked_from") == sid,
            f"forked_from={artifacts.get('fork_get_forked_from')!r}, expected {sid!r}",
        ),
        _ok(
            "forked session appears in list_sessions after fork",
            artifacts.get("fork_in_list_after_fork") is True,
            f"fork {forked_sid!r} not visible in list_sessions snapshot",
        ),
        _ok(
            "delete_session(original) returns True",
            delete_ok is True,
            f"got {delete_ok!r}",
        ),
        _ok(
            "original session removed from list after delete",
            sid not in ids_after_delete,
            f"{sid!r} still in list",
        ),
        _ok(
            "fork still accessible after source deletion",
            fork_get_status_after_delete == 200,
            f"GET fork -> {fork_get_status_after_delete} (should be 200 — delete must not cascade)",
        ),
    ]
    ok, detail = assertions.report(checks)
    return ok, detail, artifacts


def main() -> int:
    creds_path = Path(r"C:\Users\ASUS\.digitorn\credentials.json")
    if not creds_path.exists():
        print(f"FAIL  CLI credentials missing: {creds_path}")
        return 1
    token = json.loads(creds_path.read_text(encoding="utf-8"))["access_token"]
    client = DevClient.with_token(token)

    t0 = time.monotonic()
    try:
        ok, detail, artifacts = scenario_session_lifecycle(client)
    except Exception as exc:
        print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
        return 1
    dt = time.monotonic() - t0

    print(f"\n=== session lifecycle ({dt:.1f}s) ===")
    print(detail)
    print("artifacts:")
    for k, v in artifacts.items():
        line = f"  {k:36s} = {v!r}"
        sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
