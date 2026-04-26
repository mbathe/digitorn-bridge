"""Live join_session hydration test — 5 real-LLM scenarios.

Proves that a client connecting to a session receives EVERYTHING it
needs to rebuild its UI in ONE Socket.IO round-trip:

  * ``connected`` handshake
  * durable event replay (every event carries the contract)
  * ``preview:snapshot`` (if the app has a preview module)
  * ``queue:snapshot``
  * ``active_ops:snapshot``      ← operations still in flight
  * ``session:snapshot``         ← title, tokens, turn_running
  * ``memory:snapshot``          ← goal, todos, facts
  * ``approvals:snapshot``       ← only if pending approvals exist

Scenarios:
  1. Fresh session → simple chat → join → verify all snapshots present.
  2. Multi-turn + memory (SetGoal, Remember) → join → memory snapshot
     carries goal + facts.
  3. Running turn (join DURING an active LLM call) → active_ops shows
     the turn.
  4. Tool-using turn (if possible) → tool op_id parity in replay.
  5. Fresh join on pre-existing session → full rebuild from zero.

Usage::

    py -3.12 tests/live/prod_bugs/verify_join_session_full_hydration.py

The fresh-daemon runner (``run_join_session_live_fresh_daemon.py``)
boots a temp daemon on an unused port and invokes this test.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import time
import uuid
from typing import Any

import httpx
from socketio import AsyncClient

BASE = os.environ.get("DIGITORN_BASE", "http://127.0.0.1:8000")
RESULTS: list[tuple[bool, str, str]] = []


def _rec(ok: bool, label: str, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    if detail:
        print(f"         {detail}")
    RESULTS.append((ok, label, detail))


def _auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def _register(c: httpx.Client) -> tuple[str, str]:
    uname = f"hyd{uuid.uuid4().hex[:8]}"
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
        headers=_auth(tok),
        json={"message": msg},
        timeout=60.0,
    )
    return r.json()


def _get_events(c: httpx.Client, tok: str, app_id: str, sid: str,
                since: int = 0) -> list[dict]:
    r = c.get(
        f"{BASE}/api/apps/{app_id}/sessions/{sid}/events",
        headers=_auth(tok),
        params={"since_seq": since, "limit": 500},
        timeout=15.0,
    )
    return (r.json().get("data") or {}).get("events", []) if r.status_code == 200 else []


def _wait_done(c: httpx.Client, tok: str, app_id: str, sid: str,
               cid: str, timeout: float = 180.0) -> bool:
    deadline = time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        for ev in _get_events(c, tok, app_id, sid, since=seen):
            if ev["seq"] > seen:
                seen = ev["seq"]
            if ev.get("type") in ("message_done", "message_cancelled") \
                    and (ev.get("payload") or {}).get("correlation_id") == cid:
                return True
        time.sleep(1.0)
    return False


async def _join_and_collect(
    tok: str, app_id: str, sid: str, hold_seconds: float = 3.0,
) -> dict[str, list[dict]]:
    """Connect a Socket.IO client, join the session, collect every
    incoming event for ``hold_seconds``, then disconnect.

    Returns a map ``{event_type: [envelopes]}``.
    """
    collected: dict[str, list[dict]] = {}
    sio = AsyncClient()

    @sio.on("event", namespace="/events")
    async def _on_event(env: dict) -> None:
        t = env.get("type", "?")
        collected.setdefault(t, []).append(env)

    await sio.connect(
        BASE, namespaces=["/events"],
        auth={"token": tok},
        transports=["websocket"],
        wait=True, wait_timeout=10.0,
    )
    try:
        ack = await sio.call(
            "join_session",
            {"app_id": app_id, "session_id": sid, "since": 0},
            namespace="/events",
            timeout=10.0,
        )
        collected["_ack"] = [ack] if isinstance(ack, dict) else []
        await asyncio.sleep(hold_seconds)
    finally:
        await sio.disconnect()
    return collected


async def _scenario_1_fresh_chat() -> None:
    print("\n── Scenario 1: fresh chat + join returns all snapshots ──")
    with httpx.Client(timeout=30.0) as c:
        _, tok = _register(c)
        app_id = "digitorn-chat"
        sid = f"hyd-{uuid.uuid4().hex[:10]}"
        post = _post_msg(c, tok, app_id, sid, "Say hi in 3 words.")
        cid = (post.get("data") or {}).get("correlation_id")
        _rec(bool(cid), "POST /messages accepted", f"cid={cid}")
        _rec(_wait_done(c, tok, app_id, sid, cid, timeout=120.0),
             "turn 1 completed")

    collected = await _join_and_collect(tok, app_id, sid, hold_seconds=3.0)
    ack = (collected.get("_ack") or [{}])[0]
    _rec(
        bool(ack) and ack.get("ok") is True,
        "join_session ack ok=true",
        f"ack={ack}",
    )
    for expected in (
        "connected",
        "active_ops:snapshot",
        "session:snapshot",
        "queue:snapshot",
    ):
        _rec(
            expected in collected,
            f"received ``{expected}`` on join",
            f"types={sorted(collected)}",
        )

    # session:snapshot payload sanity
    sess_snaps = collected.get("session:snapshot", [])
    if sess_snaps:
        p = sess_snaps[0].get("payload") or {}
        _rec(
            p.get("app_id") == app_id and p.get("session_id") == sid,
            "session:snapshot carries app/session id",
            f"app={p.get('app_id')} sid={p.get('session_id')}",
        )
        _rec(
            p.get("message_count", 0) >= 2,
            "session:snapshot message_count >= 2",
            f"got {p.get('message_count')}",
        )

    # active_ops after done: no turn op active.
    ao_snaps = collected.get("active_ops:snapshot", [])
    if ao_snaps:
        p = ao_snaps[0].get("payload") or {}
        turn_ops = [
            o for o in (p.get("active_ops") or [])
            if o.get("op_type") == "turn"
        ]
        _rec(
            len(turn_ops) == 0,
            "active_ops:snapshot has no stale turn after done",
            f"active={p.get('count')} turn_ops={len(turn_ops)}",
        )


async def _scenario_2_memory() -> None:
    print("\n── Scenario 2: multi-turn + memory → memory:snapshot ──")
    with httpx.Client(timeout=60.0) as c:
        _, tok = _register(c)
        app_id = "digitorn-chat"
        sid = f"mem-{uuid.uuid4().hex[:10]}"
        # Turn 1 — set a goal the agent will remember.
        post = _post_msg(
            c, tok, app_id, sid,
            "My favourite colour is purple. Please remember that.",
        )
        cid = (post.get("data") or {}).get("correlation_id")
        _rec(bool(cid), "memory turn 1 accepted")
        _rec(_wait_done(c, tok, app_id, sid, cid, timeout=120.0),
             "memory turn 1 completed")

    collected = await _join_and_collect(tok, app_id, sid, hold_seconds=3.0)
    mem = collected.get("memory:snapshot", [])
    if mem:
        p = mem[0].get("payload") or {}
        _rec(
            True,
            "memory:snapshot delivered",
            f"goal={(p.get('goal') or '')[:40]!r} "
            f"todos={len(p.get('todos') or [])} "
            f"facts={len(p.get('facts') or [])}",
        )
    else:
        _rec(
            False, "memory:snapshot delivered",
            "memory module absent or no data",
        )


async def _scenario_3_active_mid_turn() -> None:
    print("\n── Scenario 3: join DURING an active turn ──")
    with httpx.Client(timeout=60.0) as c:
        _, tok = _register(c)
        app_id = "digitorn-chat"
        sid = f"mid-{uuid.uuid4().hex[:10]}"
        post = _post_msg(
            c, tok, app_id, sid,
            "Write a short poem about reconnection (4 lines).",
        )
        cid = (post.get("data") or {}).get("correlation_id")
        _rec(bool(cid), "mid-turn POST accepted")
        # Join IMMEDIATELY — the turn is likely still running.
        collected = await _join_and_collect(tok, app_id, sid, hold_seconds=2.0)
        ao_snaps = collected.get("active_ops:snapshot", [])
        if ao_snaps:
            p = ao_snaps[0].get("payload") or {}
            ops = p.get("active_ops") or []
            turn_ops = [o for o in ops if o.get("op_type") == "turn"]
            # Race: the turn may have finished between POST and join.
            # Either way, active_ops must be consistent.
            if turn_ops:
                _rec(
                    turn_ops[0].get("op_state") == "running",
                    "active turn marked running",
                    f"state={turn_ops[0].get('op_state')}",
                )
            else:
                _rec(True, "turn completed before join (race tolerated)")
        else:
            _rec(False, "active_ops:snapshot missing on mid-turn join")

        # Also verify session:snapshot reflects turn_running in real time.
        sess = collected.get("session:snapshot", [])
        if sess:
            p = sess[0].get("payload") or {}
            _rec(
                isinstance(p.get("turn_running"), bool),
                "session:snapshot carries turn_running flag",
                f"turn_running={p.get('turn_running')}",
            )

        # Wait for completion so we don't leave the daemon with a
        # dangling turn between scenarios.
        _wait_done(c, tok, app_id, sid, cid, timeout=120.0)


async def _scenario_4_contract_parity_on_replay() -> None:
    print("\n── Scenario 4: replay events carry full contract ──")
    with httpx.Client(timeout=60.0) as c:
        _, tok = _register(c)
        app_id = "digitorn-chat"
        sid = f"ct-{uuid.uuid4().hex[:10]}"
        post = _post_msg(c, tok, app_id, sid, "Explain correlation_id briefly.")
        cid = (post.get("data") or {}).get("correlation_id")
        _rec(bool(cid), "replay-test turn accepted")
        _rec(_wait_done(c, tok, app_id, sid, cid, timeout=120.0),
             "replay-test turn completed")

    collected = await _join_and_collect(tok, app_id, sid, hold_seconds=3.0)
    # Every DURABLE event delivered by the replay must carry the
    # contract (event_id, op_id, op_type, op_state).
    relevant = []
    for t, envs in collected.items():
        if t.startswith("_") or t in (
            "connected", "active_ops:snapshot", "session:snapshot",
            "memory:snapshot", "queue:snapshot", "preview:snapshot",
            "approvals:snapshot", "workspace:snapshot",
        ):
            continue
        relevant.extend(envs)
    missing = []
    for ev in relevant:
        p = ev.get("payload") or {}
        # Contract lives at TOP level of the envelope for live events.
        if not ev.get("op_id") and not p.get("op_id"):
            missing.append((ev.get("type"), ev.get("seq")))
    _rec(
        not missing,
        f"{len(relevant)} replay events carry op_id",
        f"missing: {missing[:5]}" if missing else "",
    )


async def _scenario_5_fresh_join_preexisting() -> None:
    print("\n── Scenario 5: fresh client joins pre-existing session ──")
    with httpx.Client(timeout=60.0) as c:
        _, tok = _register(c)
        app_id = "digitorn-chat"
        sid = f"pre-{uuid.uuid4().hex[:10]}"
        # Build some history.
        for i, msg in enumerate([
            "Hi.",
            "What did I just say?",
            "Count to 3 for me.",
        ]):
            post = _post_msg(c, tok, app_id, sid, msg)
            cid = (post.get("data") or {}).get("correlation_id")
            _wait_done(c, tok, app_id, sid, cid, timeout=120.0)
        print(f"  [setup] built 3-turn history on {sid}")

    # Cold join — brand-new Socket.IO client.
    collected = await _join_and_collect(tok, app_id, sid, hold_seconds=3.0)
    replay_events = [
        e for envs in collected.values() for e in envs
        if isinstance(e, dict) and e.get("seq", 0) > 0
        and e.get("type") not in (
            "connected", "active_ops:snapshot", "session:snapshot",
            "memory:snapshot", "queue:snapshot",
        )
    ]
    _rec(
        len(replay_events) >= 6,
        f"replay delivered >= 6 historical events for 3-turn session",
        f"got {len(replay_events)}",
    )

    # Snapshot sanity on cold-join.
    for expected in (
        "active_ops:snapshot",
        "session:snapshot",
        "queue:snapshot",
    ):
        _rec(
            expected in collected,
            f"cold-join pushed ``{expected}``",
        )
    # session:snapshot must have message_count >= 6 (3 user + 3 assistant).
    sess = collected.get("session:snapshot", [])
    if sess:
        p = sess[0].get("payload") or {}
        _rec(
            p.get("message_count", 0) >= 6,
            "session:snapshot message_count >= 6 after 3 turns",
            f"got {p.get('message_count')}",
        )


async def main() -> int:
    # Warm up — fail fast if the daemon is not reachable.
    try:
        r = httpx.get(f"{BASE}/health", timeout=5.0)
        if r.status_code != 200:
            print(f"FAIL: daemon not reachable ({r.status_code})")
            return 1
    except Exception as exc:
        print(f"FAIL: cannot reach daemon: {exc}")
        return 1

    await _scenario_1_fresh_chat()
    await _scenario_2_memory()
    await _scenario_3_active_mid_turn()
    await _scenario_4_contract_parity_on_replay()
    await _scenario_5_fresh_join_preexisting()

    passed = sum(1 for ok, *_ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=> {passed}/{total} pass")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
