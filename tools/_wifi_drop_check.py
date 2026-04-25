"""Simulates a wifi drop mid-turn: open stream A, receive some events,
disconnect, wait while turn continues server-side, reconnect stream B with
since_seq, verify no event is lost and the turn completes normally.
"""
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient
from digitorn.testing.assertions import sort_by_seq
from digitorn.testing.models import SessionHandle

OUT = Path(__file__).parent / "_wifi_drop_result.txt"
OUT.write_text("", encoding="utf-8")

def log(msg):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")
        f.flush()

c = DevClient()
sid = f"wifi-{uuid.uuid4().hex[:8]}"
s = SessionHandle(session_id=sid, app_id="digitorn-chat", daemon_url=c.daemon_url, workspace="")
log(f"session={sid}")

msg = (
    "Liste 25 capitales africaines, une par ligne, avec une description "
    "historique détaillée (15 mots minimum par capitale). Sois verbeux."
)
post = c.post_message_raw(s, msg)
cid = (post.get("body") or {}).get("data", {}).get("correlation_id") or ""
log(f"POST cid={cid}")

log("\n--- Stream A: connect, collect some events, then drop ---")
stream_a = c.open_event_stream(s)
stream_a.wait_for("token", timeout=20)
time.sleep(1.0)
events_a = sort_by_seq(stream_a.events())
last_seq_a = max((e.get("seq") or 0) for e in events_a)
log(f"stream A collected {len(events_a)} events, last_seq={last_seq_a}")

log("simulating network drop (stopping stream A)...")
stream_a.stop(timeout=2.0)
log("stream A disconnected")

log("waiting 5s while turn continues server-side...")
time.sleep(5.0)

log("\n--- Stream B: reconnect with since_seq ---")
from digitorn.testing.events import LiveEventStream
stream_b = LiveEventStream(
    daemon_url=c.daemon_url,
    token=c._get_auth_token(),
    app_id=s.app_id,
    session_id=s.session_id,
    since_seq=last_seq_a,
)
stream_b.start()
try:
    done = stream_b.wait_for(
        "message_done", timeout=90,
        predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
    )
    log(f"stream B saw message_done? {done is not None}")

    time.sleep(1.0)
    events_b = sort_by_seq(stream_b.events())
    log(f"stream B collected {len(events_b)} events")

    seqs_a = {e.get("seq") for e in events_a if e.get("type") != "connected"}
    seqs_b = {e.get("seq") for e in events_b if e.get("type") != "connected"}
    union = seqs_a | seqs_b

    persistent = c.get_persistent_events(s)
    persistent_seqs = {e.get("seq") for e in persistent}
    log(f"persistent DB events for session: {len(persistent_seqs)} seqs")

    missing_in_stream = persistent_seqs - union
    dup_in_stream = seqs_a & {
        e.get("seq") for e in events_b
        if (e.get("payload") or {}).get("correlation_id") == cid
    }

    log(f"\n=== coverage analysis ===")
    log(f"  stream A seqs: {len(seqs_a)}")
    log(f"  stream B seqs: {len(seqs_b)} (with since_seq={last_seq_a})")
    log(f"  union A∪B: {len(union)}")
    log(f"  persistent DB: {len(persistent_seqs)}")
    log(f"  missing from client streams: {len(missing_in_stream)}")
    log(f"  duplicated across A and B: {len(dup_in_stream)}")

    summ = c._get(f"/api/apps/{s.app_id}/sessions/{s.session_id}").json().get("data", {})
    log(f"\nsession: is_active={summ.get('is_active')} turn_count={summ.get('turn_count')}")

    def has(cid_target, etype, events):
        return any(
            e.get("type") == etype and (e.get("payload") or {}).get("correlation_id") == cid_target
            for e in events
        )
    all_events_seen = events_a + events_b
    seen_user_message = has(cid, "user_message", all_events_seen)
    seen_message_done = has(cid, "message_done", all_events_seen)

    log(f"\n=== checks ===")
    log(f"  user_message seen by client: {seen_user_message}")
    log(f"  message_done seen by client: {seen_message_done}")
    log(f"  no missing persistent events: {len(missing_in_stream) == 0}")
    log(f"  turn completed (not active): {not summ.get('is_active')}")

    verdict = (
        seen_user_message and seen_message_done
        and len(missing_in_stream) == 0
        and not summ.get("is_active")
    )
    log(f"\nVERDICT: {'PASS' if verdict else 'FAIL'}")
finally:
    stream_b.stop(timeout=2.0)
