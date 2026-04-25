"""Test queue modes: default (enqueue), merge_or_enqueue, replace_last_or_enqueue."""
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
    client = DevClient.with_token(token, auto_approve=False)
    app_id = "digitorn-chat"
    sid = f"queue-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(session_id=sid, app_id=app_id,
                            daemon_url=client.daemon_url, workspace="")
    bugs: list[str] = []
    art: dict = {"session_id": sid}
    stream = None

    try:
        # Turn 1: a slow-ish first message (will take ~5s)
        r1 = client.post_message_raw(session,
            "Please count from 1 to 10, one number per line, in your answer."
        )
        cid1 = (r1.get("body") or {}).get("data", {}).get("correlation_id", "")
        stream = client.open_event_stream(session)
        art["turn1_cid"] = cid1

        # Immediately enqueue 3 more messages (while turn 1 is running)
        time.sleep(0.2)
        r2 = client.post_message_raw(session, "A", queue_mode=None)  # default enqueue
        cid2 = (r2.get("body") or {}).get("data", {}).get("correlation_id", "")
        r3 = client.post_message_raw(session, "B", queue_mode="merge_or_enqueue")
        cid3 = (r3.get("body") or {}).get("data", {}).get("correlation_id", "")
        r4 = client.post_message_raw(session, "C", queue_mode="replace_last_or_enqueue")
        cid4 = (r4.get("body") or {}).get("data", {}).get("correlation_id", "")
        art["cids"] = {"turn1": cid1, "enqueue": cid2, "merge": cid3, "replace": cid4}

        # Immediately get the queue state
        q = client.get_queue(session, include_finished=True)
        art["queue_snapshot"] = q

        # Wait for all messages to complete (or idle)
        stream.wait_until_idle(quiet_seconds=8.0, total_timeout=180.0)

        time.sleep(1.0)
        # Final queue state
        q_final = client.get_queue(session, include_finished=True)
        art["queue_final"] = q_final

        # Full history
        hist = client.get_history(session)
        user_msgs = [m for m in hist if m.get("role") == "user"]
        assistant_msgs = [m for m in hist if m.get("role") == "assistant" and m.get("content")]
        art["n_user_msgs"] = len(user_msgs)
        art["n_assistant_msgs"] = len(assistant_msgs)
        art["user_contents"] = [str(m.get("content",""))[:50] for m in user_msgs]
        art["assistant_contents"] = [str(m.get("content",""))[:100] for m in assistant_msgs]

        # Expected: 4 user messages were sent, but merge/replace may have collapsed some
        # Print for the human to judge
        if len(user_msgs) == 4:
            art["queue_behavior"] = "no-collapse"
        elif len(user_msgs) < 4:
            art["queue_behavior"] = "collapsed"
        else:
            art["queue_behavior"] = f"unexpected count {len(user_msgs)}"

        events = assertions.sort_by_seq(stream.events())
        art["total_events"] = len(events)
        art["event_types"] = sorted({e.get("type") for e in events})
        ok, detail = assertions.seq_unique(events)
        if not ok:
            bugs.append(f"seq_unique: {detail}")

        # done event count should match the number of distinct turns that actually ran
        done_count = sum(1 for e in events if e.get("type") == "message_done")
        art["message_done_count"] = done_count
        if done_count < 1:
            bugs.append("No message_done at all")

    except Exception as e:
        bugs.append(f"EXCEPTION: {type(e).__name__}: {e}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    return (len(bugs) == 0), bugs, art


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}\nQUEUE MODES: {'PASS' if ok else 'FAIL (bugs, but may still be useful info)'}\n{'=' * 60}")
    for i, b in enumerate(bugs, 1):
        print(f"  {i}. {b}")
    print("\nARTIFACTS:", json.dumps(art, indent=2, default=str)[:5000])
    sys.exit(0 if ok else 1)
