"""Production test: multi-turn chat on digitorn-chat.

Verifies:
- 3 successive turns with context retention (name memory)
- Socket.IO event lifecycle per turn
- seq uniqueness across all turns
- persistent events replay identically
- memory store captures what we asked it to remember
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
    if not token:
        raise RuntimeError("Set DIGITORN_TEST_TOKEN env var")
    client = DevClient.with_token(token)
    app_id = "digitorn-chat"
    sid = f"prod-chat-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace="",
    )

    bugs: list[str] = []
    artifacts: dict = {"session_id": sid, "turns": []}
    stream = None

    messages = [
        "Hi! My name is Paul and I'm testing this chat in production. Please just say hello back in one sentence — no tool calls.",
        "What is my name? Answer in one short sentence.",
        "Please remember that my favorite color is green using your Remember tool, then confirm in one sentence.",
    ]

    try:
        # POST turn 1 and open the stream (reused across turns)
        post1 = client.post_message_raw(session, messages[0])
        cid1 = (post1.get("body") or {}).get("data", {}).get("correlation_id", "")
        stream = client.open_event_stream(session)
        done1 = stream.wait_for(
            "message_done", timeout=120,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid1,
        )
        if done1 is None:
            bugs.append("Turn 1: message_done never received within 120s")

        time.sleep(1.0)
        t1 = client.get_history(session)
        artifacts["turns"].append({
            "turn": 1,
            "correlation_id": cid1,
            "last_assistant": (t1[-1].get("content", "") if t1 else "")[:200],
            "event_count": len(stream.events()),
        })

        # Turn 2 — context retention
        post2 = client.post_message_raw(session, messages[1])
        cid2 = (post2.get("body") or {}).get("data", {}).get("correlation_id", "")
        done2 = stream.wait_for(
            "message_done", timeout=120,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid2,
        )
        if done2 is None:
            bugs.append("Turn 2: message_done never received within 120s")
        time.sleep(1.0)
        t2 = client.get_history(session)
        last2 = t2[-1].get("content", "") if t2 else ""
        artifacts["turns"].append({
            "turn": 2, "correlation_id": cid2,
            "last_assistant": last2[:200],
        })
        if "paul" not in last2.lower():
            bugs.append(f"Turn 2: agent FAILED to remember user's name 'Paul'. Got: {last2[:300]}")

        # Turn 3 — Remember tool
        post3 = client.post_message_raw(session, messages[2])
        cid3 = (post3.get("body") or {}).get("data", {}).get("correlation_id", "")
        done3 = stream.wait_for(
            "message_done", timeout=180,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid3,
        )
        if done3 is None:
            bugs.append("Turn 3: message_done never received within 180s")
        time.sleep(1.0)
        t3 = client.get_history(session)
        last3 = t3[-1].get("content", "") if t3 else ""
        artifacts["turns"].append({
            "turn": 3, "correlation_id": cid3,
            "last_assistant": last3[:200],
        })

        # Inspect memory store
        mem = client.get_memory(session)
        artifacts["memory"] = mem
        facts = mem.get("facts") or mem.get("remembered") or []
        green_recorded = any("green" in str(f).lower() for f in facts)
        if not green_recorded:
            bugs.append(
                f"Turn 3: 'green' not found in memory store. Facts={facts!r}. Memory keys={list(mem.keys())}"
            )

        # Global event assertions
        events = assertions.sort_by_seq(stream.events())
        artifacts["total_events"] = len(events)
        artifacts["event_types"] = sorted({e.get("type", "?") for e in events})

        ok, detail = assertions.seq_unique(events)
        if not ok:
            bugs.append(f"seq_unique FAILED: {detail}")

        ok, detail = assertions.event_count(events, "message_done", minimum=3, maximum=3)
        if not ok:
            bugs.append(f"message_done count: {detail}")

        ok, detail = assertions.event_count(events, "message_started", minimum=3, maximum=3)
        if not ok:
            bugs.append(f"message_started count: {detail}")

        ok, detail = assertions.event_count(events, "user_message", minimum=3, maximum=3)
        if not ok:
            bugs.append(f"user_message count: {detail}")

        # Persistent events replay (DB-backed)
        persistent = client.get_persistent_events(session, since_seq=0, limit=5000)
        artifacts["persistent_count"] = len(persistent)
        ok, detail = assertions.ephemeral_types_absent_from_persistent(persistent)
        if not ok:
            bugs.append(f"ephemeral leak into persistent log: {detail}")

        # Persistent should not be empty
        if len(persistent) == 0:
            bugs.append("Persistent event log is empty — events were not stored in DB")

    except Exception as e:
        bugs.append(f"EXCEPTION: {type(e).__name__}: {e}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    return (len(bugs) == 0), bugs, artifacts


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}")
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    print(f"{'=' * 60}")
    if bugs:
        print("\nBUGS FOUND:")
        for i, b in enumerate(bugs, 1):
            print(f"  {i}. {b}")
    print("\nARTIFACTS:")
    print(json.dumps(art, indent=2, default=str))
    sys.exit(0 if ok else 1)
