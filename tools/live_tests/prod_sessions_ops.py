"""Test advanced session ops: fork, compact, export, delete, resume, search."""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

from digitorn.testing import DevClient
from digitorn.testing.models import SessionHandle


def _send_simple(client: DevClient, session: SessionHandle, msg: str, timeout: float = 90) -> bool:
    post = client.post_message_raw(session, msg)
    cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
    stream = client.open_event_stream(session)
    try:
        done = stream.wait_for(
            "message_done", timeout=timeout,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
        )
        return done is not None
    finally:
        stream.stop(timeout=1.0)


def run() -> tuple[bool, list[str], dict]:
    token = os.environ.get("DIGITORN_TEST_TOKEN", "")
    client = DevClient.with_token(token)
    app_id = "digitorn-chat"
    bugs: list[str] = []
    art: dict = {}

    # Create session, 2 turns
    sid = f"ops-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(session_id=sid, app_id=app_id, daemon_url=client.daemon_url, workspace="")
    _send_simple(client, session, "Hi, remember that the magic word is BANANA. Reply with the word you remembered only.")
    _send_simple(client, session, "Just say hi.")

    # 1. EXPORT
    exp = client.export_session(session)
    art["export_keys"] = list(exp.keys()) if isinstance(exp, dict) else type(exp).__name__
    art["export_size"] = len(json.dumps(exp, default=str))
    if not exp:
        bugs.append("export_session returned empty dict")
    elif "messages" not in exp and "history" not in exp and "data" not in exp:
        # Check nested
        if not any(k in str(exp) for k in ["BANANA", "magic word"]):
            bugs.append(f"export doesn't contain session content. Keys={list(exp.keys()) if isinstance(exp,dict) else '?'}")

    # 2. FORK
    fork_result = client.fork_session(session)
    art["fork_result"] = fork_result
    forked_sid = fork_result.get("new_session_id") or fork_result.get("session_id") or ""
    if not forked_sid:
        bugs.append(f"fork_session returned no new sid. Got: {fork_result}")
    elif forked_sid == sid:
        bugs.append(f"fork returned SAME sid {sid}")
    else:
        # Verify forked session has the memory
        forked = SessionHandle(session_id=forked_sid, app_id=app_id, daemon_url=client.daemon_url, workspace="")
        forked_history = client.get_history(forked)
        has_content = any("BANANA" in str(m.get("content", "")) for m in forked_history)
        if not has_content:
            bugs.append(f"Forked session {forked_sid} doesn't inherit BANANA from parent")
        art["forked_msg_count"] = len(forked_history)

    # 3. COMPACT
    compact = client.compact_session(session)
    art["compact_result"] = compact
    if not compact:
        bugs.append("compact_session returned empty (may be a no-op at low pressure, but should return status)")

    # 4. SEARCH
    search = client.search_sessions(app_id, "BANANA", limit=10)
    art["search_hits"] = [s.get("session_id", "") for s in search] if isinstance(search, list) else search
    if sid not in (art["search_hits"] if isinstance(art["search_hits"], list) else []):
        bugs.append(f"search_sessions('BANANA') did not find session {sid}. Got: {art['search_hits']}")

    # 5. DELETE session
    delete_ok = client.delete_session(session)
    art["delete_ok"] = delete_ok
    # Confirm it's gone
    r = client._get(f"/api/apps/{app_id}/sessions/{sid}")
    art["post_delete_status"] = r.status_code
    if r.status_code not in (404, 410):
        bugs.append(f"After delete, session still reachable with status {r.status_code}")

    return (len(bugs) == 0), bugs, art


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}\nSESSION OPS: {'PASS' if ok else 'FAIL'}\n{'=' * 60}")
    for i, b in enumerate(bugs, 1):
        print(f"  {i}. {b}")
    print("\nARTIFACTS:", json.dumps(art, indent=2, default=str)[:3500])
    sys.exit(0 if ok else 1)
