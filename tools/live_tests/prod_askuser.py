"""Test AskUser interactive flow on digitorn-chat.

1. Ask agent: "Use AskUser to ask me if I prefer blue or red, with choices."
2. Poll /approvals until we see the pending ask_user request
3. Answer via approve(response="blue") - simulates human picking 'blue'
4. Verify agent receives the answer and completes

This is the critical human-in-the-loop UX.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


def run() -> tuple[bool, list[str], dict]:
    token = os.environ.get("DIGITORN_TEST_TOKEN", "")
    client = DevClient.with_token(token, auto_approve=False)  # we'll respond manually
    app_id = "digitorn-chat"
    sid = f"ask-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(session_id=sid, app_id=app_id,
                            daemon_url=client.daemon_url, workspace="")
    bugs: list[str] = []
    art: dict = {"session_id": sid}
    stream = None

    try:
        msg = (
            "I need your help. Call the AskUser tool with question='Blue or red?' "
            "and choices=['blue','red']. Then tell me what I picked in one sentence."
        )
        post = client.post_message_raw(session, msg)
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        stream = client.open_event_stream(session)

        # Wait for approval_request event
        ask_evt = stream.wait_for("approval_request", timeout=60,
            predicate=lambda e: "ask" in str((e.get("payload") or {}).get("data", {})).lower() or True)
        if ask_evt is None:
            bugs.append("No approval_request event received within 60s - AskUser tool never triggered")
            return False, bugs, art

        art["approval_event_payload"] = str(ask_evt.get("payload", {}))[:500]

        # Get pending via API
        time.sleep(0.5)
        pending = client.get_pending(app_id)
        art["pending_count"] = len(pending)
        art["pending_types"] = [p.get("type") or p.get("tool_name") for p in pending]

        ask_req = None
        for p in pending:
            if "ask" in str(p.get("tool_name", "")).lower() or "ask" in str(p.get("type", "")).lower():
                ask_req = p
                break
        if not ask_req:
            bugs.append(f"No ask_user in pending. Got: {art['pending_types']}")
            return False, bugs, art

        art["ask_request_id"] = ask_req.get("request_id")
        art["ask_tool_name"] = ask_req.get("tool_name")
        art["ask_params"] = ask_req.get("tool_params")

        # Respond: user picks "blue"
        rid = ask_req.get("request_id", "")
        ok = client.respond_to_ask(app_id, rid, "blue")
        art["respond_ok"] = ok
        if not ok:
            bugs.append(f"respond_to_ask returned False for rid={rid}")

        # Wait for completion
        done = stream.wait_for("message_done", timeout=120,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid)
        art["done_received"] = done is not None
        if done is None:
            bugs.append("message_done never received after answering AskUser")

        # Check final answer
        time.sleep(1.0)
        hist = client.get_history(session)
        last_a = next((m.get("content","") for m in reversed(hist) if m.get("role")=="assistant" and m.get("content")), "")
        art["last_assistant"] = last_a[:300]
        if "blue" not in last_a.lower():
            bugs.append(f"Agent didn't acknowledge 'blue' in final answer. Got: {last_a[:200]}")

        events = assertions.sort_by_seq(stream.events())
        ok_seq, detail = assertions.seq_unique(events)
        if not ok_seq:
            bugs.append(f"seq_unique: {detail}")

    except Exception as e:
        bugs.append(f"EXCEPTION: {type(e).__name__}: {e}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    return (len(bugs) == 0), bugs, art


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}\nASKUSER: {'PASS' if ok else 'FAIL'}\n{'=' * 60}")
    for i, b in enumerate(bugs, 1):
        print(f"  {i}. {b}")
    print("\nARTIFACTS:", json.dumps(art, indent=2, default=str)[:3000])
    sys.exit(0 if ok else 1)
