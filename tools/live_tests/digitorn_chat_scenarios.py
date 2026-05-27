"""Live E2E scenarios for the builtin `digitorn-chat` app.

No mocks. Real daemon, real LLM (whatever the daemon configured as default).
Acts like a human user: open a session, type a message, wait for the answer,
inspect events + history + cleanup state.

Run:
    py -3.12 tools/live_tests/digitorn_chat_scenarios.py

Returns exit code 0 on PASS, 1 on FAIL. Prints a structured report.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Make `import digitorn.testing` work from a checkout.
sys.path.insert(0, str(Path("C:/Users/ASUS/Documents/digitorn-bridge/packages")))

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


APP_ID = "digitorn-chat"


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def scenario_chat_smoke(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    """One real turn against digitorn-chat, full lifecycle + durability check."""
    artifacts: dict[str, Any] = {}

    # --- preflight ---
    health = client.get_health()
    artifacts["health"] = health
    apps = client.list_apps()
    artifacts["app_count"] = len(apps) if isinstance(apps, list) else (apps.get("count") if isinstance(apps, dict) else None)
    deployed_ids = {
        a.get("app_id") for a in (apps if isinstance(apps, list) else apps.get("rows", []))
        if isinstance(a, dict)
    }
    if APP_ID not in deployed_ids:
        return False, f"app '{APP_ID}' not deployed (found: {sorted(deployed_ids)[:10]})", artifacts

    # --- session ---
    sid = f"chat-smoke-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid,
        app_id=APP_ID,
        daemon_url=client.daemon_url,
        workspace="",
    )
    artifacts["session_id"] = sid

    user_msg = "Reply with the single word OK."

    stream = None
    try:
        stream = client.send_live(session, user_msg, total_timeout=120)
        wire_events = stream.events()
        events = assertions.sort_by_seq(wire_events)
        artifacts["event_count"] = len(events)
        artifacts["event_types"] = sorted({e["type"] for e in events})

        # --- durability check (persistent eligible types only) ---
        persistent = client.get_persistent_events(session, since_seq=0)
        if isinstance(persistent, dict):
            persistent_rows = persistent.get("events") or persistent.get("rows") or []
        else:
            persistent_rows = persistent
        artifacts["persistent_count"] = len(persistent_rows)

        # --- queue cleanup ---
        queue = client.get_queue(session, include_finished=False)
        if isinstance(queue, dict):
            queue_rows = queue.get("entries") or queue.get("rows") or []
        else:
            queue_rows = queue
        artifacts["queue_pending"] = len(queue_rows)

        # user_message MUST be persisted (it carries the user's input).
        user_message_in_persistent = any(
            (e.get("type") if isinstance(e, dict) else None) == "user_message"
            for e in persistent_rows
        )

        checks = [
            _ok("daemon healthy", bool(health), f"health={health!r}"),
            _ok("app deployed", APP_ID in deployed_ids, "missing"),
            _ok("events received", len(events) > 0, "stream returned 0 events"),
            ("seq_unique", assertions.seq_unique(events)),
            (
                "live lifecycle order (assistant turn)",
                assertions.event_order(events, ["message_started", "message_done"]),
            ),
            _ok("user_message persisted", user_message_in_persistent,
                "user_message not found in persistent events"),
            _ok("queue drained", len(queue_rows) == 0, f"{len(queue_rows)} pending"),
            (
                "ephemeral not persisted",
                assertions.ephemeral_types_absent_from_persistent(persistent_rows),
            ),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, artifacts
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def _new_session(client: DevClient, tag: str) -> SessionHandle:
    sid = f"chat-{tag}-{uuid.uuid4().hex[:8]}"
    return SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )


def _persistent_rows(client: DevClient, session: SessionHandle) -> list[dict[str, Any]]:
    res = client.get_persistent_events(session, since_seq=0)
    if isinstance(res, dict):
        return res.get("events") or res.get("rows") or []
    return res or []


def _queue_pending(client: DevClient, session: SessionHandle) -> int:
    q = client.get_queue(session, include_finished=False)
    rows = (q.get("entries") or q.get("rows") or []) if isinstance(q, dict) else (q or [])
    return len(rows)


def scenario_multi_turn(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    """3 turns in the same session: history grows, each turn has its own correlation_id."""
    artifacts: dict[str, Any] = {}
    session = _new_session(client, "multi")
    artifacts["session_id"] = session.session_id

    prompts = [
        "Reply with the single word ONE.",
        "Reply with the single word TWO.",
        "Reply with the single word THREE.",
    ]
    correlation_ids: list[str] = []
    per_turn_done = []

    stream = None
    try:
        for i, msg in enumerate(prompts, start=1):
            stream = client.send_live(session, msg, total_timeout=120, stream=stream)
            # Capture the message_done for THIS turn (last one in events).
            events = assertions.sort_by_seq(stream.events())
            dones = [e for e in events if e["type"] == "message_done"]
            per_turn_done.append(len(dones))
            if dones:
                cid = (dones[-1].get("payload") or {}).get("correlation_id") or ""
                if cid and cid not in correlation_ids:
                    correlation_ids.append(cid)

        artifacts["correlation_ids"] = correlation_ids
        artifacts["per_turn_done_cumulative"] = per_turn_done

        persistent = _persistent_rows(client, session)
        artifacts["persistent_count"] = len(persistent)
        user_messages = [e for e in persistent if (e.get("type") if isinstance(e, dict) else None) == "user_message"]
        artifacts["persistent_user_messages"] = len(user_messages)

        queue_pending = _queue_pending(client, session)
        artifacts["queue_pending"] = queue_pending

        # message_done count should grow monotonically with each turn.
        monotonic_dones = all(
            per_turn_done[i] >= per_turn_done[i - 1]
            for i in range(1, len(per_turn_done))
        )

        checks = [
            _ok("3 distinct correlation_ids", len(correlation_ids) == 3,
                f"got {len(correlation_ids)}: {correlation_ids}"),
            _ok("message_done count monotonic across turns", monotonic_dones,
                f"counts={per_turn_done}"),
            # Per-turn, the daemon persists 2 user_message rows by design:
            # one live event (kind=session) emitted at POST time, and one
            # history record (kind=message) written by save_messages at
            # turn end. So 3 turns produce >= 3 (typically 6).
            _ok("user_message persisted at least once per turn",
                len(user_messages) >= len(prompts),
                f"got {len(user_messages)} for {len(prompts)} prompts"),
            _ok("queue drained after 3 turns", queue_pending == 0,
                f"{queue_pending} pending"),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, artifacts
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def scenario_abort_mid_turn(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    """Send a long prompt, abort while it's streaming, verify interrupted state + queue drained."""
    import threading
    artifacts: dict[str, Any] = {}
    session = _new_session(client, "abort")
    artifacts["session_id"] = session.session_id

    long_prompt = (
        "Write a long, detailed five-paragraph essay about the history "
        "of Lisbon, the architecture of Alfama, the recipes of pastel de "
        "nata, and the music of fado. Be very thorough."
    )

    stream = None
    abort_result: dict[str, Any] = {}

    def _delayed_abort() -> None:
        time.sleep(1.2)  # let the turn start streaming
        try:
            abort_result["resp"] = client.abort_session(session, purge_queue=True)
        except Exception as exc:
            abort_result["error"] = f"{type(exc).__name__}: {exc}"

    try:
        stream = client.open_event_stream(session, wait_for_session=False) if False else None
        # Use send_live but with short total_timeout so we exit fast after abort.
        t = threading.Thread(target=_delayed_abort, daemon=True)
        try:
            t.start()
            stream = client.send_live(session, long_prompt, total_timeout=30)
        finally:
            t.join(timeout=10)

        events = assertions.sort_by_seq(stream.events())
        artifacts["event_count"] = len(events)
        artifacts["abort_response"] = abort_result

        # Look for any abort/interrupted/cancelled signal.
        interrupted_signal = any(
            e["type"] in ("abort", "session_aborted", "turn_cancelled")
            or (e.get("payload") or {}).get("interrupted") is True
            or (e.get("payload") or {}).get("status") == "interrupted"
            for e in events
        )
        artifacts["interrupted_signal"] = interrupted_signal

        # Give the daemon a moment to settle and drain the queue.
        time.sleep(1.5)
        queue_pending = _queue_pending(client, session)
        artifacts["queue_pending"] = queue_pending

        persistent = _persistent_rows(client, session)
        artifacts["persistent_count"] = len(persistent)

        checks = [
            _ok("abort_session ok", abort_result.get("error") is None,
                f"abort error: {abort_result.get('error')}"),
            _ok("interrupted signal observed", interrupted_signal,
                "no abort/interrupted/cancelled event in stream"),
            _ok("queue drained after abort", queue_pending == 0,
                f"{queue_pending} pending"),
            _ok("user_message persisted (even on abort)",
                any((e.get("type") if isinstance(e, dict) else None) == "user_message"
                    for e in persistent),
                "user_message missing"),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, artifacts
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def scenario_resume_after_abort(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    """Abort turn 1, then send turn 2 in the same session; verify both are in history."""
    import threading
    artifacts: dict[str, Any] = {}
    session = _new_session(client, "resume")
    artifacts["session_id"] = session.session_id

    stream = None
    try:
        # --- Turn 1: long, then abort ---
        def _abort_after_delay() -> None:
            time.sleep(1.2)
            try:
                client.abort_session(session, purge_queue=True)
            except Exception:
                pass

        t = threading.Thread(target=_abort_after_delay, daemon=True)
        t.start()
        stream = client.send_live(
            session,
            "Write a long, detailed essay about Lisbon's history. Be thorough.",
            total_timeout=30,
        )
        t.join(timeout=10)
        time.sleep(1.0)  # let abort settle

        events_after_turn1 = len(assertions.sort_by_seq(stream.events()))
        artifacts["events_after_turn1"] = events_after_turn1

        # --- Turn 2: short, in the SAME session ---
        stream = client.send_live(
            session,
            "Reply with the single word RESUMED.",
            total_timeout=60,
            stream=stream,
        )

        events = assertions.sort_by_seq(stream.events())
        artifacts["event_count_total"] = len(events)

        # Look for a message_done for turn 2 (after the abort).
        dones = [e for e in events if e["type"] == "message_done"]
        artifacts["message_done_total"] = len(dones)

        persistent = _persistent_rows(client, session)
        user_messages = [e for e in persistent if (e.get("type") if isinstance(e, dict) else None) == "user_message"]
        artifacts["persistent_user_messages"] = len(user_messages)

        queue_pending = _queue_pending(client, session)
        artifacts["queue_pending"] = queue_pending

        checks = [
            _ok("turn 2 produced at least one message_done", len(dones) >= 1,
                f"got {len(dones)}"),
            _ok("both turns' user_message persisted", len(user_messages) >= 2,
                f"got {len(user_messages)} (expected >= 2)"),
            _ok("queue drained after resume", queue_pending == 0,
                f"{queue_pending} pending"),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, artifacts
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def main() -> int:
    creds_path = Path(r"C:\Users\ASUS\.digitorn\credentials.json")
    if not creds_path.exists():
        print(f"FAIL  CLI credentials missing: {creds_path}")
        return 1
    token = json.loads(creds_path.read_text(encoding="utf-8"))["access_token"]
    client = DevClient.with_token(token)

    scenarios = [
        ("chat_smoke", scenario_chat_smoke),
        ("multi_turn", scenario_multi_turn),
        ("abort_mid_turn", scenario_abort_mid_turn),
        ("resume_after_abort", scenario_resume_after_abort),
    ]

    overall_ok = True
    for name, fn in scenarios:
        t0 = time.monotonic()
        try:
            ok, detail, artifacts = fn(client)
        except Exception as exc:
            print(f"\n=== {name} (crash) ===")
            print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
            overall_ok = False
            continue
        dt = time.monotonic() - t0
        print(f"\n=== {name} ({dt:.1f}s) ===")
        print(detail)
        print("artifacts:")
        for k, v in artifacts.items():
            print(f"  {k:32s} = {v!r}")
        if not ok:
            overall_ok = False

    print("\n=== OVERALL ===")
    print("PASS" if overall_ok else "FAIL")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
