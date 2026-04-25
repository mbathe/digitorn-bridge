"""Reusable end-to-end scenarios for live testing.

Each scenario returns (ok: bool, detail: str, artifacts: dict) so a
runner can aggregate outcomes and inspect raw data on failure.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from digitorn.testing import assertions
from digitorn.testing.client import DevClient
from digitorn.testing.events import LiveEventStream
from digitorn.testing.models import SessionHandle


def _new_session(app_id: str, daemon_url: str, prefix: str = "s") -> SessionHandle:
    sid = f"{prefix}-{uuid.uuid4().hex[:8]}"
    return SessionHandle(
        session_id=sid, app_id=app_id, daemon_url=daemon_url, workspace="",
    )


def scenario_single_turn(client: DevClient, app_id: str) -> tuple[bool, str, dict]:
    session = _new_session(app_id, client.daemon_url, "single")
    stream = None
    try:
        stream = client.send_live(session, "Dis bonjour en 3 mots.", total_timeout=60)
        events = stream.events()
        sorted_events = assertions.sort_by_seq(events)
        checks = [
            ("seq_unique", assertions.seq_unique(events)),
            ("user_message once", assertions.event_count(events, "user_message", 1, 1)),
            ("message_started once", assertions.event_count(events, "message_started", 1, 1)),
            ("message_done once", assertions.event_count(events, "message_done", 1, 1)),
            ("lifecycle order (by seq)", assertions.event_order(
                sorted_events, ["user_message", "message_started", "message_done"],
            )),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, {"session": session.session_id, "event_count": len(events)}
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def scenario_queue_burst(
    client: DevClient, app_id: str, n_messages: int = 5,
) -> tuple[bool, str, dict]:
    session = _new_session(app_id, client.daemon_url, "burst")
    stream = None
    try:
        correlation_ids: list[str] = []
        for i in range(n_messages):
            r = client.post_message_raw(session, f"msg {i}: dis juste 'ok {i}'.")
            cid = (r.get("body") or {}).get("data", {}).get("correlation_id") or ""
            correlation_ids.append(cid)

        fp = [c for c in correlation_ids if c.startswith("fp-")]
        queued = [c for c in correlation_ids if c and not c.startswith("fp-")]

        stream = client.open_event_stream(session)
        for _ in range(60):
            q = client.get_queue(session)
            active = [e for e in q if e.get("status") in ("queued", "running")]
            if not active:
                break
            time.sleep(1.0)
        events = stream.events()

        checks = [
            ("exactly 1 fast-path", (len(fp) == 1, f"fp={fp}")),
            ("rest queued", (len(queued) == n_messages - 1, f"queued={len(queued)}")),
            ("queue drained", (not client.get_queue(session), "queue empty")),
            ("seq monotonic", assertions.seq_is_monotonic(events)),
            ("user_message per msg", assertions.event_count(
                events, "user_message", n_messages, n_messages,
            )),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, {
            "session": session.session_id,
            "correlation_ids": correlation_ids,
            "event_count": len(events),
        }
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def scenario_abort_preserves_queue(
    client: DevClient, app_id: str,
) -> tuple[bool, str, dict]:
    session = _new_session(app_id, client.daemon_url, "abort")
    stream = None
    try:
        client.post_message_raw(session, "Liste 20 capitales africaines en détail.")
        time.sleep(0.3)
        r2 = client.post_message_raw(session, "Ensuite dis juste 'BONJOUR'.")
        cid2 = (r2.get("body") or {}).get("data", {}).get("correlation_id") or ""

        time.sleep(1.0)
        stream = client.open_event_stream(session)
        client.abort_session(session, purge_queue=False)

        time.sleep(2.0)
        q_after_abort = client.get_queue(session)
        msg2_survived = any(e.get("correlation_id") == cid2 for e in q_after_abort)

        stream.wait_for(
            "message_done", timeout=90,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid2,
        )

        q_final = client.get_queue(session)
        events = stream.events()
        abort_events = [e for e in events if e.get("type") == "abort"]

        checks = [
            ("msg2 survived abort", (msg2_survived, f"q_after_abort={[e.get('correlation_id') for e in q_after_abort]}")),
            ("queue drained after resume", (not q_final, "queue empty")),
            ("abort event emitted", (len(abort_events) >= 1, f"abort_events={len(abort_events)}")),
            ("seq monotonic", assertions.seq_is_monotonic(events)),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, {
            "session": session.session_id,
            "abort_count": len(abort_events),
        }
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def scenario_cross_session_isolation(
    client: DevClient, app_id: str,
) -> tuple[bool, str, dict]:
    s_a = _new_session(app_id, client.daemon_url, "isoA")
    s_b = _new_session(app_id, client.daemon_url, "isoB")
    stream_a = None
    stream_b = None
    try:
        client.post_message_raw(s_a, "Dis 'SESSION_A'.")
        client.post_message_raw(s_b, "Dis 'SESSION_B'.")

        stream_a = client.open_event_stream(s_a)
        stream_b = client.open_event_stream(s_b)
        stream_a.wait_for("message_done", timeout=60)
        stream_b.wait_for("message_done", timeout=60)

        events_a = stream_a.events()
        events_b = stream_b.events()

        a_contents = [
            (e.get("payload") or {}).get("content", "")
            for e in events_a if e.get("type") == "user_message"
        ]
        b_contents = [
            (e.get("payload") or {}).get("content", "")
            for e in events_b if e.get("type") == "user_message"
        ]

        a_leak = any("SESSION_B" in c for c in a_contents)
        b_leak = any("SESSION_A" in c for c in b_contents)

        a_sids = {
            e.get("session_id") for e in events_a
            if e.get("type") in ("user_message", "token", "message_done")
        }
        b_sids = {
            e.get("session_id") for e in events_b
            if e.get("type") in ("user_message", "token", "message_done")
        }

        checks = [
            ("no content leak A->B", (not a_leak, f"a_contents={a_contents}")),
            ("no content leak B->A", (not b_leak, f"b_contents={b_contents}")),
            ("A stream only sees session A", (a_sids == {s_a.session_id} or not a_sids, f"a_sids={a_sids}")),
            ("B stream only sees session B", (b_sids == {s_b.session_id} or not b_sids, f"b_sids={b_sids}")),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, {"session_a": s_a.session_id, "session_b": s_b.session_id}
    finally:
        if stream_a is not None:
            stream_a.stop(timeout=2.0)
        if stream_b is not None:
            stream_b.stop(timeout=2.0)


def scenario_persistent_log_contract(
    client: DevClient, app_id: str,
) -> tuple[bool, str, dict]:
    """Ephemeral events (token, thinking_delta) must NOT end up in session_events."""
    session = _new_session(app_id, client.daemon_url, "persist")
    stream = None
    try:
        stream = client.send_live(session, "Dis bonjour en 3 mots.", total_timeout=60)
        time.sleep(1.0)
        persistent = client.get_persistent_events(session)
        live_tokens = len(stream.events_by_type("token"))
        persistent_tokens = sum(1 for e in persistent if e.get("type") == "token")

        checks = [
            ("persistent log has user_message", (
                any(e.get("type") == "user_message" for e in persistent),
                f"types in persistent: {sorted({e.get('type') for e in persistent})}",
            )),
            ("persistent log has message_done", (
                any(e.get("type") == "message_done" for e in persistent),
                "",
            )),
            ("tokens ephemeral (absent from persistent)",
                assertions.ephemeral_types_absent_from_persistent(persistent)),
            ("live saw tokens", (live_tokens >= 1, f"live_tokens={live_tokens}")),
            ("persistent stored 0 tokens", (persistent_tokens == 0, f"persistent_tokens={persistent_tokens}")),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, {
            "session": session.session_id,
            "persistent_count": len(persistent),
            "live_tokens": live_tokens,
        }
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def run_all(client: DevClient, app_id: str) -> dict[str, tuple[bool, str, dict]]:
    results: dict[str, tuple[bool, str, dict]] = {}
    for name, fn in [
        ("single_turn", scenario_single_turn),
        ("queue_burst", scenario_queue_burst),
        ("cross_session_isolation", scenario_cross_session_isolation),
        ("persistent_log_contract", scenario_persistent_log_contract),
        ("abort_preserves_queue", scenario_abort_preserves_queue),
    ]:
        t0 = time.time()
        try:
            ok, detail, art = fn(client, app_id)
        except Exception as exc:
            ok, detail, art = False, f"EXCEPTION: {type(exc).__name__}: {exc}", {}
        dur = time.time() - t0
        results[name] = (ok, detail, {**art, "duration_seconds": round(dur, 1)})
    return results


if __name__ == "__main__":
    import sys
    app_id = sys.argv[1] if len(sys.argv) > 1 else "digitorn-chat"
    client = DevClient()
    results = run_all(client, app_id)
    passed = 0
    for name, (ok, detail, art) in results.items():
        tag = "PASS" if ok else "FAIL"
        print(f"\n[{tag}] {name} ({art.get('duration_seconds')}s)")
        print(detail)
        if not ok:
            print(f"  artifacts: {art}")
        if ok:
            passed += 1
    print(f"\n{passed}/{len(results)} scenarios passed")
    if passed < len(results):
        sys.exit(1)
