"""Strict abort leak check: after abort, NO event (except the abort event itself
and message_cancelled) should fire for the aborted turn. Checks ALL event types,
waits longer, dumps everything.
"""
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient
from digitorn.testing.assertions import sort_by_seq
from digitorn.testing.models import SessionHandle

OUT = Path(__file__).parent / "_abort_leak_strict_result.txt"
OUT.write_text("", encoding="utf-8")

def log(msg):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")
        f.flush()

_ALLOWED_POST_ABORT = {"abort", "message_cancelled"}

c = DevClient()
sid = f"abs-{uuid.uuid4().hex[:8]}"
s = SessionHandle(session_id=sid, app_id="digitorn-chat", daemon_url=c.daemon_url, workspace="")
log(f"session={sid}")

msg = (
    "Liste 50 capitales africaines, une par ligne, avec une description "
    "historique très détaillée. Au moins 20 mots par capitale. Sois très verbeux."
)
post = c.post_message_raw(s, msg)
cid = (post.get("body") or {}).get("data", {}).get("correlation_id") or ""
log(f"POST status={post.get('status_code')} cid={cid}")

stream = c.open_event_stream(s)
try:
    first_token = stream.wait_for("token", timeout=20)
    log(f"first token: seq={first_token.get('seq') if first_token else None}")
    if first_token is None:
        log("FAIL: no token before abort")
        raise SystemExit(1)

    time.sleep(0.5)
    pre_abort_events = stream.events()
    log(f"events before abort: {len(pre_abort_events)}")

    abort_ack = c.abort_session(s)
    log(f"abort ack: {abort_ack}")

    log("waiting 8s for any late events...")
    time.sleep(8.0)

    all_events = sort_by_seq(stream.events())

    abort_idx = None
    for i, e in enumerate(all_events):
        if e.get("type") == "abort":
            abort_idx = i
            break

    if abort_idx is None:
        log("FAIL: abort event never arrived")
        raise SystemExit(1)

    post_abort = all_events[abort_idx + 1:]
    log(f"total events: {len(all_events)} / post-abort: {len(post_abort)}")
    log("")
    log("=== all post-abort events (RAW) ===")
    leaks: list[dict] = []
    for e in post_abort:
        et = e.get("type", "?")
        pl = e.get("payload") or {}
        ecid = pl.get("correlation_id") or ""
        seq = e.get("seq")
        summary = f"seq={seq} type={et} cid={ecid[:16]}"
        log(f"  {summary}")
        if et in _ALLOWED_POST_ABORT:
            continue
        if et.startswith("preview:") or et in ("hook", "queue:snapshot", "agent_cancel", "notification", "notification_result"):
            continue
        if ecid and ecid != cid:
            continue
        leaks.append({"type": et, "seq": seq, "cid": ecid})

    log("")
    log(f"=== leaks ({len(leaks)}) ===")
    for x in leaks:
        log(f"  LEAK {x}")

    summ = c._get(f"/api/apps/{s.app_id}/sessions/{s.session_id}").json().get("data", {})
    log(f"\nsession is_active: {summ.get('is_active')} interrupted: {summ.get('interrupted')}")
    log(f"queue: {len(c.get_queue(s))} entries")
    verdict = "PASS" if not leaks else "FAIL"
    log(f"\nVERDICT: {verdict}")
finally:
    stream.stop(timeout=2.0)
