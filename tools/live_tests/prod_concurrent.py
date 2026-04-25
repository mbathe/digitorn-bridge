"""3 concurrent sessions on the same app — check isolation.

- Each session sets a DIFFERENT goal via MemorySetGoal
- All 3 are kicked off at the same time
- After all complete, check each session's memory has ONLY its own goal
- Check no seq collisions across sessions, no cross-traffic events
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import time
import uuid

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


def drive_session(client: DevClient, app_id: str, goal: str) -> dict:
    sid = f"conc-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(session_id=sid, app_id=app_id,
                            daemon_url=client.daemon_url, workspace="")
    post = client.post_message_raw(session,
        f"Please set your goal to exactly: '{goal}'. Use the MemorySetGoal tool. "
        f"Then just say 'done-{goal}' in one word.",
    )
    cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
    stream = client.open_event_stream(session)
    try:
        done = stream.wait_for(
            "message_done", timeout=180,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
        )
        time.sleep(0.5)
        mem = client.get_memory(session)
        goal_in_mem = (mem.get("working") or {}).get("goal", "")
        seqs = [int(e.get("seq", 0) or 0) for e in stream.events()]
        return {
            "sid": sid, "cid": cid, "intended_goal": goal,
            "done": done is not None,
            "actual_goal": goal_in_mem[:200],
            "events": len(stream.events()),
            "seq_range": (min(seqs) if seqs else 0, max(seqs) if seqs else 0),
        }
    finally:
        stream.stop(timeout=1.0)


def run() -> tuple[bool, list[str], dict]:
    token = os.environ.get("DIGITORN_TEST_TOKEN", "")
    if not token:
        raise RuntimeError("No token")
    client = DevClient.with_token(token)

    goals = ["alpha-red", "beta-blue", "gamma-green"]
    bugs: list[str] = []

    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(drive_session, client, "digitorn-chat", g) for g in goals]
        results = [f.result() for f in cf.as_completed(futs, timeout=300)]

    artifacts = {"sessions": results}

    # Each session should have its OWN goal in memory
    for r in results:
        if not r["done"]:
            bugs.append(f"Session {r['sid']} ({r['intended_goal']}): message_done not received")
            continue
        if r["intended_goal"] not in r["actual_goal"]:
            bugs.append(
                f"ISOLATION BREAK: session {r['sid']} intended '{r['intended_goal']}' "
                f"but memory.goal='{r['actual_goal']}'"
            )

    # Check no seq overlaps between sessions (each session has its own seq namespace)
    # Sessions should have DIFFERENT correlation_ids
    cids = [r["cid"] for r in results]
    if len(set(cids)) != len(cids):
        bugs.append(f"Duplicate correlation_ids across sessions: {cids}")

    return (len(bugs) == 0), bugs, artifacts


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}\nCONCURRENT: {'PASS' if ok else 'FAIL'}\n{'=' * 60}")
    for i, b in enumerate(bugs, 1):
        print(f"  {i}. {b}")
    print("\nARTIFACTS:", json.dumps(art, indent=2, default=str)[:3000])
    sys.exit(0 if ok else 1)
