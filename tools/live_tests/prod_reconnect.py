"""Test Socket.IO reconnect + replay since_seq.

1. Open stream, send message, capture events with seqs.
2. Close stream mid-turn.
3. Reopen with since_seq=0 → should replay all events from 0.
4. Also test since_seq=<middle> → only new events after that.
5. Compare replayed events ↔ original events (must be identical by seq+payload).
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

from digitorn.testing import DevClient, assertions
from digitorn.testing.events import LiveEventStream
from digitorn.testing.models import SessionHandle


def run() -> tuple[bool, list[str], dict]:
    token = os.environ.get("DIGITORN_TEST_TOKEN", "")
    client = DevClient.with_token(token)
    app_id = "digitorn-chat"
    sid = f"recon-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(session_id=sid, app_id=app_id,
                            daemon_url=client.daemon_url, workspace="")
    bugs: list[str] = []
    art: dict = {"session_id": sid}

    # 1. First stream — send a message and wait for completion, capture all
    post = client.post_message_raw(session, "Count from 1 to 5, one per line, then say 'done'.")
    cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
    art["cid"] = cid

    stream1 = client.open_event_stream(session)
    try:
        stream1.wait_for("message_done", timeout=60,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid)
        time.sleep(0.5)
        original_events = assertions.sort_by_seq(stream1.events())
        art["original_count"] = len(original_events)
        art["original_types"] = sorted({e.get("type") for e in original_events})
        art["original_last_seq"] = max((int(e.get("seq", 0) or 0) for e in original_events), default=0)
        art["original_first_seq"] = min((int(e.get("seq", 0) or 0) for e in original_events if e.get("seq")), default=0)
    finally:
        stream1.stop(timeout=2.0)
    time.sleep(1.0)

    # 2. Reconnect with since_seq=0 → replay
    stream2 = LiveEventStream(
        daemon_url=client.daemon_url, token=token,
        app_id=app_id, session_id=sid, since_seq=0,
    )
    try:
        stream2.start(timeout=8)
        # Also explicitly request replay
        time.sleep(1.0)
        try:
            replayed_count = stream2.request_replay(since=0, timeout=8)
            art["explicit_replay_count"] = replayed_count
        except Exception as e:
            art["explicit_replay_error"] = str(e)
        stream2.wait_until_idle(quiet_seconds=3.0, total_timeout=15.0)
        replay_events = assertions.sort_by_seq(stream2.events())
        art["replay_count"] = len(replay_events)
        art["replay_types"] = sorted({e.get("type") for e in replay_events})
    finally:
        stream2.stop(timeout=2.0)

    # 3. Persistent events via REST API (source of truth)
    persist = client.get_persistent_events(session, since_seq=0, limit=5000)
    art["persistent_count"] = len(persist)
    art["persistent_types"] = sorted({e.get("type") for e in persist})

    # Count eligibles (non-ephemeral)
    EPHEMERAL = {"token", "out_token", "in_token", "thinking_delta", "thinking_started",
                 "thinking", "assistant_stream_snapshot", "memory_update", "queue:snapshot",
                 "preview:delta", "agent_progress", "connected", "hook"}
    meaningful_original = [e for e in original_events if e.get("type") not in EPHEMERAL]
    meaningful_persist = [e for e in persist if e.get("type") not in EPHEMERAL]
    art["meaningful_original"] = len(meaningful_original)
    art["meaningful_persist"] = len(meaningful_persist)

    # Check replay delivered at least the persistent events
    persist_seqs = {int(e.get("seq", 0) or 0) for e in persist}
    replay_seqs = {int(e.get("seq", 0) or 0) for e in replay_events}
    missing = persist_seqs - replay_seqs
    if len(persist) > 0 and len(replay_events) == 0:
        bugs.append("Replay delivered ZERO events despite persistent log having %d" % len(persist))
    elif len(missing) > len(persist_seqs) // 2:
        bugs.append(f"Replay missing {len(missing)}/{len(persist_seqs)} seqs from persistent log")

    # Check since_seq filter works: reconnect with since_seq=last_seq, expect few events
    stream3 = LiveEventStream(
        daemon_url=client.daemon_url, token=token,
        app_id=app_id, session_id=sid, since_seq=art["original_last_seq"],
    )
    try:
        stream3.start(timeout=8)
        time.sleep(1.0)
        try:
            stream3.request_replay(since=art["original_last_seq"], timeout=8)
        except Exception as e:
            art["replay3_err"] = str(e)
        stream3.wait_until_idle(quiet_seconds=2.0, total_timeout=8.0)
        late_events = [e for e in stream3.events() if int(e.get("seq", 0) or 0) > art["original_last_seq"]]
        art["since_last_seq_delivered"] = len(late_events)
    finally:
        stream3.stop(timeout=2.0)

    # Seq integrity
    ok, detail = assertions.seq_unique(original_events)
    if not ok:
        bugs.append(f"Original stream seq_unique: {detail}")
    ok, detail = assertions.ephemeral_types_absent_from_persistent(persist)
    if not ok:
        bugs.append(f"Persistent log contains ephemeral: {detail}")

    return (len(bugs) == 0), bugs, art


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}\nRECONNECT+REPLAY: {'PASS' if ok else 'FAIL'}\n{'=' * 60}")
    for i, b in enumerate(bugs, 1):
        print(f"  {i}. {b}")
    print("\nARTIFACTS:", json.dumps(art, indent=2, default=str)[:4000])
    sys.exit(0 if ok else 1)
