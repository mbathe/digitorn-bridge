"""After abort, verify no more turn events (tokens, tool_calls, stream_done, message_done
for that correlation_id) arrive. The abort must cleanly stop the turn server-side.
"""
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient
from digitorn.testing.assertions import sort_by_seq
from digitorn.testing.models import SessionHandle

OUT = Path(__file__).parent / "_abort_leak_result.txt"
OUT.write_text("", encoding="utf-8")

def log(msg):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")
        f.flush()

c = DevClient()
sid = f"abl-{uuid.uuid4().hex[:8]}"
s = SessionHandle(session_id=sid, app_id="digitorn-chat", daemon_url=c.daemon_url, workspace="")

log(f"session={sid}")

msg = (
    "Liste 30 capitales africaines, une par ligne avec une description "
    "historique détaillée. Sois très verbeux, au moins 10 mots par capitale."
)
post = c.post_message_raw(s, msg)
cid = (post.get("body") or {}).get("data", {}).get("correlation_id") or ""
log(f"POST status={post.get('status_code')} cid={cid}")

stream = c.open_event_stream(s)
try:
    first_token = stream.wait_for("token", timeout=20)
    log(f"first token arrived? {first_token is not None} seq={first_token.get('seq') if first_token else None}")
    if first_token is None:
        log("FAIL: no token received - can't test abort leak")
        raise SystemExit(1)

    time.sleep(0.3)
    pre_abort_count = len(stream.events())
    log(f"events just before abort: {pre_abort_count}")

    abort_ack = c.abort_session(s)
    log(f"abort ack: {abort_ack}")
    abort_ts = time.monotonic()

    stream.wait_for("abort", timeout=5)
    log(f"abort event received")

    time.sleep(3.0)

    events = sort_by_seq(stream.events())
    post_abort = []
    abort_seen = False
    for e in events:
        if e.get("type") == "abort":
            abort_seen = True
            continue
        if not abort_seen:
            continue
        et = e.get("type")
        payload = e.get("payload") or {}
        e_cid = payload.get("correlation_id") or ""
        if et in ("token", "tool_call", "tool_start", "tool_end", "stream_done", "message_done", "result", "assistant_stream_snapshot"):
            if e_cid == cid or not e_cid:
                post_abort.append({"type": et, "seq": e.get("seq"), "cid": e_cid})

    log(f"total events: {len(events)}")
    log(f"events after abort (turn-related, same cid): {len(post_abort)}")
    for p in post_abort[:20]:
        log(f"  LEAK: {p}")

    summ = c._get(f"/api/apps/{s.app_id}/sessions/{s.session_id}").json().get("data", {})
    log(f"session is_active after abort: {summ.get('is_active')}")
    log(f"session interrupted: {summ.get('interrupted')}")
    q = c.get_queue(s)
    log(f"queue after abort: {len(q)} entries")

    verdict = "PASS" if len(post_abort) == 0 else f"FAIL - {len(post_abort)} leaked events"
    log(f"\nVERDICT: {verdict}")
finally:
    stream.stop(timeout=2.0)
