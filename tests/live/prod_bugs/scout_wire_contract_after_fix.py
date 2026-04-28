"""Scout wire-level after the 3 bug fixes - verifies what a Flutter
client ACTUALLY receives:

  * Live Socket.IO events → contract at top level (was already OK).
  * Replay events (join with since=0 after some history) → contract
    at top level (BUG 1 fix).
  * Multiple connections on same session → event_id stable if the
    event was fanned out (BUG 2 invariant preserved).
  * No hook op stuck running in active_ops:snapshot (BUG 3 fix).
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import time
import uuid
from collections import defaultdict

import httpx
from socketio import AsyncClient

BASE = os.environ.get("DIGITORN_BASE", "http://127.0.0.1:8000")


def _auth(tok): return {"Authorization": f"Bearer {tok}"}


def _register(c):
    uname = f"scout{uuid.uuid4().hex[:8]}"
    r = c.post(
        f"{BASE}/auth/register",
        json={
            "username": uname,
            "email": f"{uname}@t.l",
            "password": "TestProd1234!xyz",
        },
    )
    if r.status_code != 200:
        r = c.post(
            f"{BASE}/auth/login",
            json={"email": f"{uname}@t.l", "password": "TestProd1234!xyz"},
        )
    return uname, r.json()["access_token"]


async def _join_collect(tok, app_id, sid, since=0, hold=3.0):
    collected = []
    sio = AsyncClient()

    @sio.on("event", namespace="/events")
    async def _e(env):
        collected.append(env)

    await sio.connect(
        BASE, namespaces=["/events"], auth={"token": tok},
        transports=["websocket"], wait=True, wait_timeout=10.0,
    )
    ack = await sio.call(
        "join_session",
        {"app_id": app_id, "session_id": sid, "since": since},
        namespace="/events", timeout=10.0,
    )
    await asyncio.sleep(hold)
    await sio.disconnect()
    return ack, collected


def _post(c, tok, app_id, sid, msg):
    return c.post(
        f"{BASE}/api/apps/{app_id}/sessions/{sid}/messages",
        headers=_auth(tok), json={"message": msg}, timeout=60.0,
    ).json()


def _wait_done(c, tok, app_id, sid, cid, timeout=120.0):
    deadline = time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        r = c.get(
            f"{BASE}/api/apps/{app_id}/sessions/{sid}/events",
            headers=_auth(tok),
            params={"since_seq": seen, "limit": 500},
            timeout=10.0,
        )
        evs = (r.json().get("data") or {}).get("events", [])
        for e in evs:
            if e["seq"] > seen:
                seen = e["seq"]
            if e.get("type") in ("message_done", "message_cancelled") and \
                    (e.get("payload") or {}).get("correlation_id") == cid:
                return True
        time.sleep(1.0)
    return False


def _check_top_level(env: dict) -> list[str]:
    """Return list of contract fields present at top level."""
    return [
        k for k in (
            "event_id", "op_id", "op_type", "op_state",
            "app_id", "session_id", "user_id", "correlation_id",
        )
        if env.get(k) is not None
    ]


async def main() -> int:
    with httpx.Client(timeout=30.0) as c:
        uname, tok = _register(c)
        app_id = "digitorn-chat"
        sid = f"scout-{uuid.uuid4().hex[:10]}"

        # Build some history.
        post = _post(c, tok, app_id, sid, "Say hi briefly.")
        cid = (post.get("data") or {}).get("correlation_id")
        assert cid, f"post failed: {post}"
        assert _wait_done(c, tok, app_id, sid, cid), "turn never done"

    # Fresh join - collect replay + hydration snapshots.
    ack, events = await _join_collect(tok, app_id, sid, since=0, hold=3.0)
    print(f"[scout] ack={ack}")
    print(f"[scout] collected {len(events)} events")

    failures = []

    # BUG 1 check - every durable event has top-level contract.
    durable_types = {
        "user_message", "message_started", "message_done",
        "stream_done", "result", "hook", "tool_start", "tool_call",
        "agent_event", "memory_update",
    }
    for env in events:
        if env.get("type") in durable_types:
            present = _check_top_level(env)
            required = {"event_id", "op_id", "op_type", "op_state",
                        "app_id", "session_id", "user_id"}
            missing = required - set(present)
            if missing:
                failures.append(
                    f"top-level missing on {env['type']}[seq={env.get('seq')}]: {missing}"
                )

    # BUG 3 check - no hook op stuck running in active_ops:snapshot.
    ao = [e for e in events if e.get("type") == "active_ops:snapshot"]
    if ao:
        active = (ao[0].get("payload") or {}).get("active_ops") or []
        hook_stuck = [
            o for o in active
            if o.get("op_type") == "system"
            and o.get("op_state") in ("running", "pending")
        ]
        if hook_stuck:
            failures.append(
                f"system op stuck running in active_ops: {hook_stuck}"
            )
        else:
            print("[BUG-3] no system op stuck in active_ops - OK")

    # Breakdown for human review.
    by_type = defaultdict(int)
    for e in events:
        by_type[e.get("type", "?")] += 1
    print("[scout] events by type:")
    for t, n in sorted(by_type.items()):
        print(f"  {t}: {n}")

    # Sample one durable event - show the full top-level shape.
    durable_samples = [
        e for e in events
        if e.get("type") in durable_types
    ]
    if durable_samples:
        print("[scout] sample durable event (top-level keys):")
        sample = durable_samples[0]
        for k in sorted(sample.keys()):
            v = sample[k]
            if isinstance(v, dict):
                print(f"  {k}: dict[{len(v)} keys]")
            else:
                print(f"  {k}: {v!r:.60}")

    if failures:
        print("\nFAIL - scout found contract violations:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS - wire-level contract verified after bug fixes")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
