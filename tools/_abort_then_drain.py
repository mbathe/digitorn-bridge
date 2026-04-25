"""After abort with preserve_queue (default), the next queued msg must
be auto-injected. Frontend must see: message_cancelled(msg1) → message_started(msg2)
→ tokens(msg2) → message_done(msg2), all in seq order, queue empty at end.
"""
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient
from digitorn.testing.assertions import sort_by_seq
from digitorn.testing.models import SessionHandle

OUT = Path(__file__).parent / "_abort_then_drain_result.txt"
OUT.write_text("", encoding="utf-8")

def log(msg):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")
        f.flush()

c = DevClient()
sid = f"atd-{uuid.uuid4().hex[:8]}"
s = SessionHandle(session_id=sid, app_id="digitorn-chat", daemon_url=c.daemon_url, workspace="")
log(f"session={sid}")

msg1 = (
    "Liste 50 capitales africaines détaillées, au moins 15 mots par capitale. "
    "Sois très verbeux."
)
msg2 = "Dis juste 'INJECTED_OK' et rien d'autre."

r1 = c.post_message_raw(s, msg1)
cid1 = (r1.get("body") or {}).get("data", {}).get("correlation_id") or ""
log(f"POST msg1 cid={cid1}")

time.sleep(0.3)
r2 = c.post_message_raw(s, msg2)
cid2 = (r2.get("body") or {}).get("data", {}).get("correlation_id") or ""
log(f"POST msg2 cid={cid2} (should be queued, not fp-)")
log(f"  msg2 queued? {not cid2.startswith('fp-')}")

q_before = c.get_queue(s)
log(f"queue before abort: {len(q_before)} entries, statuses={[e.get('status') for e in q_before]}")

stream = c.open_event_stream(s)
try:
    stream.wait_for("token", timeout=20)
    log("first token of msg1 arrived")
    time.sleep(0.3)

    log("--- aborting msg1 (preserve_queue=true) ---")
    abort_ack = c.abort_session(s, purge_queue=False)
    log(f"abort ack: {abort_ack}")

    done = stream.wait_for(
        "message_done", timeout=90,
        predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid2,
    )
    log(f"message_done(msg2) arrived? {done is not None}")

    time.sleep(2.0)

    events = sort_by_seq(stream.events())

    def has_for(cid, etype):
        return any(
            e.get("type") == etype
            and (e.get("payload") or {}).get("correlation_id") == cid
            for e in events
        )

    msg1_cancelled = has_for(cid1, "message_cancelled")
    msg1_done = has_for(cid1, "message_done")
    msg2_started = has_for(cid2, "message_started")
    msg2_user = has_for(cid2, "user_message")
    msg2_done = has_for(cid2, "message_done")

    idx = {}
    for i, e in enumerate(events):
        t = e.get("type")
        pl = e.get("payload") or {}
        ecid = pl.get("correlation_id")
        key = f"{t}:{ecid}" if ecid else t
        idx.setdefault(key, i)

    log("\n=== full event log (seq, type, cid) ===")
    for e in events:
        t = e.get("type")
        pl = e.get("payload") or {}
        ecid = pl.get("correlation_id") or ""
        log(f"  seq={e.get('seq')} type={t} cid={ecid[:16]}")

    log("\n=== event indices (by seq) ===")
    for k in sorted(idx, key=idx.get):
        log(f"  {idx[k]:3d}  {k}")

    log("\n=== checks ===")
    log(f"  msg1 cancelled: {msg1_cancelled}")
    log(f"  msg1 NOT marked done: {not msg1_done}")
    log(f"  msg2 user_message: {msg2_user}")
    log(f"  msg2 started after msg1 cancelled: {msg2_started}")
    log(f"  msg2 done: {msg2_done}")

    def get_idx(key):
        return idx.get(key, -1)

    order_ok = True
    msg1_cancel_idx = get_idx(f"message_cancelled:{cid1}")
    msg2_started_idx = get_idx(f"message_started:{cid2}")
    if msg1_cancel_idx >= 0 and msg2_started_idx >= 0:
        order_ok = msg1_cancel_idx < msg2_started_idx
    log(f"  chronology msg1_cancelled < msg2_started: {order_ok}")

    q_final = c.get_queue(s)
    log(f"\nqueue at end: {len(q_final)} entries (expect 0)")

    verdict = (
        msg1_cancelled and not msg1_done and
        msg2_user and msg2_started and msg2_done and
        order_ok and len(q_final) == 0
    )
    log(f"\nVERDICT: {'PASS' if verdict else 'FAIL'}")
finally:
    stream.stop(timeout=2.0)
