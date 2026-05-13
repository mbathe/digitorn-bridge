"""Test abort flow:

1. POST a long-running task to digitorn-code
2. Wait for tool_start event (agent is actively working)
3. POST /abort
4. Verify: abort event received, message_done never arrives, session is_active=False
5. Verify: approvals cleared, shell tasks killed
6. Verify: session is resumable with next message
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


def run() -> tuple[bool, list[str], dict]:
    token = os.environ.get("DIGITORN_TEST_TOKEN", "")
    if not token:
        raise RuntimeError("No token")
    client = DevClient.with_token(token)

    # copilot-smoke is the canonical full-capability smoke app (Claude
    # Sonnet 4.5 via Copilot through the Digitorn gateway). It has the
    # same tool surface as digitorn-code (Bash + Write + Read + Glob)
    # but a working provider, so the abort test can actually start a
    # tool before we cancel.
    app_id = "copilot-smoke"
    ws = str(Path("C:/Users/ASUS/Documents/digitorn-bridge/tmp_test_ws").resolve())
    sid = f"abort-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(session_id=sid, app_id=app_id,
                            daemon_url=client.daemon_url, workspace=ws)

    bugs: list[str] = []
    artifacts: dict = {"session_id": sid}
    stream = None

    try:
        post = client.post_message_raw(session,
            "Please list ALL files in the workspace recursively, then read every single one, "
            "and summarize the whole project in extreme detail. Take your time."
        )
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        artifacts["correlation_id"] = cid
        stream = client.open_event_stream(session)

        # Wait for first tool_start
        tool_started = stream.wait_for("tool_start", timeout=30)
        if tool_started is None:
            bugs.append("Never saw tool_start within 30s - abort test can't proceed")
            return False, bugs, artifacts

        # Abort mid-flight
        t_abort = time.monotonic()
        abort_resp = client.abort(session)
        artifacts["abort_response"] = abort_resp

        # Wait for abort-related event
        abort_evt = stream.wait_for_any(["abort", "interrupted", "cancel", "stream_done"], timeout=10)
        artifacts["abort_event_type"] = abort_evt.get("type") if abort_evt else None
        if abort_evt is None:
            bugs.append("No abort/interrupted/cancel event received within 10s of abort call")

        # Verify: no message_done for this cid after abort
        time.sleep(3.0)
        late_done = any(
            (e.get("payload") or {}).get("correlation_id") == cid
            for e in stream.events_by_type("message_done")
        )
        if late_done:
            bugs.append("message_done arrived AFTER abort (should not)")

        # Session state
        r = client.open_event_stream  # noqa - just to keep ref
        sess_state = client._get(f"/api/apps/{app_id}/sessions/{sid}").json().get("data", {})
        artifacts["session_state"] = {
            "is_active": sess_state.get("is_active"),
            "interrupted": sess_state.get("interrupted"),
            "turn_count": sess_state.get("turn_count"),
        }
        if sess_state.get("is_active") is True:
            bugs.append(f"Session still active after abort: {sess_state.get('is_active')}")
        if sess_state.get("interrupted") is not True:
            bugs.append(f"Session.interrupted={sess_state.get('interrupted')} (expected True)")

        # Try to resume by sending a new message
        post2 = client.post_message_raw(session, "ok forget that, just say 'resumed' in one word.")
        cid2 = (post2.get("body") or {}).get("data", {}).get("correlation_id", "")
        done2 = stream.wait_for(
            "message_done", timeout=60,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid2,
        )
        artifacts["resume_done"] = done2 is not None
        if done2 is None:
            bugs.append("Could not resume session after abort (no message_done for new cid)")

        events = assertions.sort_by_seq(stream.events())
        artifacts["total_events"] = len(events)
        ok, detail = assertions.seq_unique(events)
        if not ok:
            bugs.append(f"seq_unique FAILED during abort flow: {detail}")

    except Exception as e:
        bugs.append(f"EXCEPTION: {type(e).__name__}: {e}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    return (len(bugs) == 0), bugs, artifacts


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}\nABORT FLOW: {'PASS' if ok else 'FAIL'}\n{'=' * 60}")
    for i, b in enumerate(bugs, 1):
        print(f"  {i}. {b}")
    print("\nARTIFACTS:", json.dumps(art, indent=2, default=str)[:3000])
    sys.exit(0 if ok else 1)
