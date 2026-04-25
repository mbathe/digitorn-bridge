"""Long multi-turn chat on digitorn-chat: 6 turns, test context pressure + auto-compact.

We load the context with chunky content, then ask about an early fact to verify
compaction didn't destroy key facts.
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
    client = DevClient.with_token(token)
    app_id = "digitorn-chat"
    sid = f"long-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(session_id=sid, app_id=app_id,
                            daemon_url=client.daemon_url, workspace="")
    bugs: list[str] = []
    art: dict = {"session_id": sid, "turns": []}
    stream = None

    LOREM = (
        "Lorem ipsum dolor sit amet consectetur adipiscing elit, "
        "sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. "
        "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris. "
    )

    turns = [
        "Hi! My SECRET_CODE is XJ7-KOALA-42. Please remember it. Reply 'noted' only.",
        f"Here is a long text to fill the context: {LOREM * 80}. Reply with 'ok' only.",
        f"Another long text: {LOREM * 80}. Reply 'ok' only.",
        f"More text: {LOREM * 80}. Reply 'ok' only.",
        f"Continuing: {LOREM * 80}. Reply 'ok' only.",
        "Now, what was my SECRET_CODE? Answer in one sentence.",
    ]

    try:
        def _turn(idx: int, msg: str, to: float = 180) -> dict:
            nonlocal stream
            post = client.post_message_raw(session, msg)
            cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
            if stream is None:
                stream = client.open_event_stream(session)
            done = stream.wait_for("message_done", timeout=to,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid)
            time.sleep(0.4)
            hist = client.get_history(session)
            last_a = ""
            for m in reversed(hist):
                if m.get("role")=="assistant" and m.get("content"):
                    last_a = m["content"]; break
            sess = client._get(f"/api/apps/{app_id}/sessions/{sid}").json().get("data", {})
            return {
                "turn": idx, "cid": cid, "done": done is not None,
                "last": last_a[:150],
                "pressure": sess.get("context", {}).get("pressure"),
                "total_tokens": sess.get("context", {}).get("total_estimated_tokens"),
                "compactions": sess.get("context", {}).get("compactions", 0),
                "turn_count": sess.get("turn_count"),
            }

        for i, m in enumerate(turns, 1):
            r = _turn(i, m)
            art["turns"].append(r)
            if not r["done"]:
                bugs.append(f"Turn {i}: no message_done")
                break

        # Final: check SECRET_CODE was retained
        last_turn = art["turns"][-1] if art["turns"] else {}
        if not last_turn:
            bugs.append("No turns recorded")
        else:
            if "XJ7-KOALA-42" not in last_turn.get("last", "") and "KOALA" not in last_turn.get("last", "").upper():
                bugs.append(f"SECRET_CODE XJ7-KOALA-42 NOT retained through chat. Last reply: {last_turn.get('last','')[:200]}")

        # Did compaction actually fire?
        final_compactions = art["turns"][-1].get("compactions", 0) if art["turns"] else 0
        art["final_compactions"] = final_compactions
        max_pressure = max((t.get("pressure") or 0 for t in art["turns"]), default=0)
        art["max_pressure_observed"] = max_pressure
        if final_compactions == 0 and max_pressure > 0.75:
            bugs.append(f"Pressure reached {max_pressure} but auto-compact never fired (compactions=0)")

        # Events: check seq unique
        if stream is not None:
            events = assertions.sort_by_seq(stream.events())
            art["total_events"] = len(events)
            ok, detail = assertions.seq_unique(events)
            if not ok:
                bugs.append(f"seq_unique: {detail}")

    except Exception as e:
        bugs.append(f"EXCEPTION: {type(e).__name__}: {e}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    return (len(bugs) == 0), bugs, art


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}\nLONG CHAT: {'PASS' if ok else 'FAIL'}\n{'=' * 60}")
    for i, b in enumerate(bugs, 1):
        print(f"  {i}. {b}")
    print("\nARTIFACTS:", json.dumps(art, indent=2, default=str)[:3500])
    sys.exit(0 if ok else 1)
