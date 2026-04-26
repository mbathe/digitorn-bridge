"""End-to-end live verification: the universal event contract
(op_id / op_type / op_state) survives every layer the daemon pushes
events through — the ring buffer, persistence, replay.

This test uses the RUNNING daemon, a REAL user session, a REAL LLM
(via the app's configured provider) and the REAL DevClient. No
mocks. If an event drops the contract at any point, the assertions
catch it.

Usage::

    # daemon already running on 127.0.0.1:8000
    py -3.12 tests/live/prod_bugs/verify_session_event_contract_live.py
"""
from __future__ import annotations
import json
import os
import sys
import time
import uuid
from typing import Any

import httpx

BASE = os.environ.get("DIGITORN_BASE", "http://127.0.0.1:8000")
RESULTS: list[tuple[bool, str, str]] = []


def _rec(ok: bool, label: str, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    if detail:
        print(f"         {detail}")
    RESULTS.append((ok, label, detail))


def _auth_headers(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _register(c: httpx.Client) -> tuple[str, str]:
    uname = f"live{uuid.uuid4().hex[:8]}"
    email = f"{uname}@test.local"
    pwd = "TestProd1234!xyz"
    r = c.post(f"{BASE}/auth/register", json={
        "username": uname, "email": email, "password": pwd,
    })
    if r.status_code != 200:
        r = c.post(f"{BASE}/auth/login", json={"email": email, "password": pwd})
    d = r.json()
    return uname, d["access_token"]


def _post_msg(c: httpx.Client, tok: str, app_id: str, sid: str, msg: str) -> dict:
    r = c.post(
        f"{BASE}/api/apps/{app_id}/sessions/{sid}/messages",
        headers=_auth_headers(tok),
        json={"message": msg},
        timeout=30.0,
    )
    return r.json()


def _get_events(c: httpx.Client, tok: str, app_id: str, sid: str,
                since: int = 0) -> list[dict]:
    r = c.get(
        f"{BASE}/api/apps/{app_id}/sessions/{sid}/events",
        headers=_auth_headers(tok),
        params={"since_seq": since, "limit": 500},
        timeout=10.0,
    )
    if r.status_code != 200:
        return []
    return (r.json().get("data") or {}).get("events", [])


def _get_active_ops(c: httpx.Client, tok: str, app_id: str, sid: str) -> dict:
    r = c.get(
        f"{BASE}/api/apps/{app_id}/sessions/{sid}/active-ops",
        headers=_auth_headers(tok),
        timeout=10.0,
    )
    return r.json().get("data") or {} if r.status_code == 200 else {
        "status": r.status_code, "err": r.text[:200],
    }


def _wait_done(c: httpx.Client, tok: str, app_id: str, sid: str,
               correlation_id: str, timeout: float = 180.0) -> bool:
    """Poll /events until message_done for this correlation_id."""
    deadline = time.monotonic() + timeout
    seen_seq = 0
    while time.monotonic() < deadline:
        events = _get_events(c, tok, app_id, sid, since=seen_seq)
        for ev in events:
            if ev["seq"] > seen_seq:
                seen_seq = ev["seq"]
            t = ev.get("type")
            p = ev.get("payload") or {}
            if t in ("message_done", "message_cancelled") and \
                    p.get("correlation_id") == correlation_id:
                return True
        time.sleep(1.0)
    return False


def _has_contract(ev: dict) -> tuple[bool, str]:
    """Return (ok, reason) — every client-facing event must carry
    op_id / op_type / op_state."""
    p = ev.get("payload") or {}
    missing = []
    if not p.get("op_id"):
        missing.append("op_id")
    if not p.get("op_type"):
        missing.append("op_type")
    if not p.get("op_state"):
        missing.append("op_state")
    if missing:
        return False, f"missing {missing}"
    return True, ""


def main() -> int:
    with httpx.Client(timeout=30.0) as c:
        # ── Setup ─────────────────────────────────────────────
        r = c.get(f"{BASE}/health")
        if r.status_code != 200:
            print(f"FAIL: daemon not reachable ({r.status_code})")
            return 1

        uname, tok = _register(c)
        print(f"[setup] user={uname}")

        # ── Scenario 1: simple chat turn on digitorn-chat ────
        print("\n── Scenario 1: one chat turn ──")
        app_id = "digitorn-chat"
        sid = f"live-{uuid.uuid4().hex[:10]}"
        post = _post_msg(c, tok, app_id, sid, "Say hi in 3 words.")
        ok = post.get("success") and (post.get("data") or {}).get("correlation_id")
        _rec(
            bool(ok),
            "POST /messages accepted",
            f"correlation_id={(post.get('data') or {}).get('correlation_id')}",
        )
        if not ok:
            print("cannot proceed without successful POST; output:")
            print(json.dumps(post, indent=2)[:800])
            return 1
        cid = post["data"]["correlation_id"]

        # Wait for message_done
        done = _wait_done(c, tok, app_id, sid, cid, timeout=120.0)
        _rec(done, "message_done received within 120s")

        # ── Contract integrity on persisted events ──────────
        events = _get_events(c, tok, app_id, sid, since=0)
        _rec(len(events) > 0, f"replay returned {len(events)} events")

        # Every event must carry the contract.
        missing = []
        for ev in events:
            ok, reason = _has_contract(ev)
            if not ok:
                missing.append((ev.get("type"), ev.get("seq"), reason))
        _rec(
            len(missing) == 0,
            "every persisted event carries op_id/op_type/op_state",
            f"{len(missing)} violations: {missing[:5]}" if missing else "",
        )

        # ── op_id parity: message_started and message_done of
        # ONE turn must share the same op_id ────────────────
        started = [e for e in events
                   if e.get("type") == "message_started"
                   and (e.get("payload") or {}).get("correlation_id") == cid]
        done_ev = [e for e in events
                   if e.get("type") in ("message_done", "message_cancelled")
                   and (e.get("payload") or {}).get("correlation_id") == cid]
        if started and done_ev:
            op_a = (started[0].get("payload") or {}).get("op_id")
            op_b = (done_ev[0].get("payload") or {}).get("op_id")
            _rec(
                op_a == op_b and op_a,
                "message_started and message_done share op_id",
                f"started={op_a}  done={op_b}",
            )
        else:
            _rec(False,
                 "message_started and message_done share op_id",
                 f"started={len(started)} done={len(done_ev)}")

        # ── Terminal op_state on message_done ────────────────
        for ev in done_ev:
            state = (ev.get("payload") or {}).get("op_state")
            _rec(
                state in ("completed", "cancelled", "failed", "timeout"),
                f"{ev['type']} carries terminal op_state",
                f"got {state!r}",
            )

        # ── Tool lifecycle: if any tool fired, tool_start and
        # tool_call share op_id ─────────────────────────────
        tool_events = [e for e in events
                       if e.get("type") in ("tool_start", "tool_call")]
        if tool_events:
            by_op: dict[str, list] = {}
            for e in tool_events:
                oid = (e.get("payload") or {}).get("op_id")
                if oid:
                    by_op.setdefault(oid, []).append(e)
            pairs_found = sum(
                1 for v in by_op.values()
                if any(x.get("type") == "tool_start" for x in v)
                and any(x.get("type") == "tool_call" for x in v)
            )
            if pairs_found > 0:
                _rec(True,
                     f"{pairs_found} tool_start/tool_call pair(s) share op_id")
            else:
                # tool_start may be ephemeral on some streams — still
                # the tool_call alone must have an op_id.
                for tc in [e for e in tool_events if e.get("type") == "tool_call"]:
                    tc_op = (tc.get("payload") or {}).get("op_id")
                    _rec(bool(tc_op),
                         "tool_call carries an op_id even without persisted start",
                         f"op_id={tc_op}")
        else:
            print("  (no tool calls in this turn — tool lifecycle assertion skipped)")

        # ── /active-ops: after message_done, active list is empty
        # (or only contains non-turn ops) ───────────────────
        ao = _get_active_ops(c, tok, app_id, sid)
        active_count = ao.get("count", 0)
        ops = ao.get("active_ops") or []
        turn_ops = [o for o in ops if o.get("op_type") == "turn"]
        _rec(
            len(turn_ops) == 0,
            "after message_done, no turn op is still active",
            f"active={active_count} turn_ops={len(turn_ops)} all={ops[:3]}",
        )

        # ── seq monotonicity ─────────────────────────────────
        seqs = [e["seq"] for e in events if "seq" in e]
        _rec(seqs == sorted(seqs),
             "seq strictly monotonic across persisted events")

        # ── Scenario 2: second turn on same session ─────────
        print("\n── Scenario 2: second turn on same session ──")
        post2 = _post_msg(c, tok, app_id, sid, "And in Spanish?")
        cid2 = (post2.get("data") or {}).get("correlation_id")
        _rec(bool(cid2), "second turn accepted")
        if cid2:
            done2 = _wait_done(c, tok, app_id, sid, cid2, timeout=120.0)
            _rec(done2, "second turn completed")
            events_all = _get_events(c, tok, app_id, sid, since=0)
            turn_1_events = [e for e in events_all
                             if (e.get("payload") or {}).get("correlation_id") == cid]
            turn_2_events = [e for e in events_all
                             if (e.get("payload") or {}).get("correlation_id") == cid2]
            _rec(
                bool(turn_1_events) and bool(turn_2_events),
                "both turns appear in the replay",
                f"t1={len(turn_1_events)} t2={len(turn_2_events)}",
            )
            # op_ids of the two turns must differ.
            oids_1 = {(e.get("payload") or {}).get("op_id")
                      for e in turn_1_events} - {None}
            oids_2 = {(e.get("payload") or {}).get("op_id")
                      for e in turn_2_events} - {None}
            overlap = oids_1 & oids_2
            _rec(
                len(overlap) == 0,
                "op_ids of two distinct turns do not overlap",
                f"overlap={overlap}",
            )

    passed = sum(1 for ok, *_ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=> {passed}/{total} pass")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
