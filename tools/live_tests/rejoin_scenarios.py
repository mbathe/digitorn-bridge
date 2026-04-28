"""Mid-turn rejoin scenarios.

Goal: reproduce the bug where reopening a session whose turn is still
running returns an incomplete and/or out-of-order history.

We post three user messages back-to-back (msg1 goes fast-path, msg2
and msg3 land in the queue) then, while msg1's turn is still
running, we:

  1. Close the event stream (simulating a disconnect).
  2. Call GET /history and GET /queue.
  3. Open a fresh event stream with since=0 to get full replay.

Three ordering sources are compared:

  - ``/history.messages``  - in-memory ``session.messages`` blob
                             (list-append order, no per-msg seq).
  - ``/history.events``    - persisted ``SessionEvent`` rows, seq asc.
  - ``/queue``             - ``SessionMessageQueue`` rows, position asc.

The scenario asserts that every posted user message is represented in
the combined view, that user_message events appear in post order, and
that the final history (after queue drains) matches the post order.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from digitorn.testing import assertions
from digitorn.testing.client import DevClient
from digitorn.testing.models import SessionHandle


_SLOW_PROMPTS = [
    "Dis bonjour dans 5 langues differentes, une par ligne.",
    "Liste 6 couleurs primaires et secondaires, une par ligne.",
    "Donne trois conseils courts pour bien dormir.",
]


def _new_session(app_id: str, daemon_url: str) -> SessionHandle:
    sid = f"rejoin-{uuid.uuid4().hex[:8]}"
    return SessionHandle(
        session_id=sid, app_id=app_id, daemon_url=daemon_url, workspace="",
    )


def _user_contents(messages: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    content = part.get("text", "")
                    break
            else:
                content = ""
        if isinstance(content, str):
            out.append(content)
    return out


def _user_event_contents(events: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for env in events:
        if env.get("type") != "user_message":
            continue
        # Accept both envelope shapes:
        #   - Socket.IO replay + new /history.events: ``payload``
        #   - Legacy KV turn log: ``data``
        body = env.get("payload") or env.get("data") or {}
        content = body.get("content", "") if isinstance(body, dict) else ""
        if isinstance(content, str):
            out.append(content)
    return out


def _queue_contents(queue_entries: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for entry in queue_entries:
        msg = entry.get("message", "")
        if isinstance(msg, str):
            out.append(msg)
    return out


def _dedup_preserve_order(xs: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in xs:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def scenario_midturn_rejoin(
    client: DevClient, app_id: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Reproduce the incomplete-history-on-mid-turn-rejoin bug."""
    session = _new_session(app_id, client.daemon_url)
    prompts = _SLOW_PROMPTS[:]
    correlation_ids: list[str] = []
    stream = None
    try:
        # Fire all three back-to-back. msg1 should go fast-path, msg2/msg3 queued.
        for prompt in prompts:
            r = client.post_message_raw(session, prompt)
            cid = (r.get("body") or {}).get("data", {}).get("correlation_id") or ""
            correlation_ids.append(cid)

        fp = [c for c in correlation_ids if c.startswith("fp-")]
        queued_ids = [c for c in correlation_ids if c and not c.startswith("fp-")]

        # === MID-TURN SNAPSHOT - while msg1 still running, msg2/msg3 queued ===
        # Give the daemon a brief moment to register the queue rows but NOT
        # long enough to drain msg1. The LLM latency is what keeps msg1
        # running while we snapshot.
        time.sleep(0.4)
        mid_messages = client.get_history(session)
        mid_events = client.get_events(session)
        mid_queue = client.get_queue(session)
        # Also grab the raw /history payload to check the new flags.
        mid_history_raw = client._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/history",
            params={"include_system": "true"},
        ).json().get("data", {}) or {}
        mid_turn_active = bool(mid_history_raw.get("turn_active"))
        mid_pending_queue = mid_history_raw.get("pending_queue") or []

        mid_user_msgs = _user_contents(mid_messages)
        mid_user_evts = _user_event_contents(mid_events)
        mid_queue_msgs = _queue_contents(mid_queue)

        # === REJOIN via fresh event stream with since=0 ===
        stream = client.open_event_stream(session)
        # Wait for the full 3-message drain. digitorn-chat + deepseek is
        # fast (~3s per short prompt), so 90s is generous.
        deadline = time.time() + 120.0
        while time.time() < deadline:
            q = client.get_queue(session)
            active = [e for e in q if e.get("status") in ("queued", "running")]
            if not active:
                break
            time.sleep(1.0)

        # Let the final message_done trail into the stream.
        time.sleep(1.0)
        replay_events = stream.events()
        final_messages = client.get_history(session)
        final_events = client.get_events(session)

        final_user_msgs = _user_contents(final_messages)
        replay_user_evts = _user_event_contents(
            assertions.sort_by_seq(replay_events)
        )
        final_user_evts = _user_event_contents(final_events)

        # === ASSERTIONS ===
        # B1. All three prompts must be present in the MID-TURN combined view.
        mid_combined = _dedup_preserve_order(
            mid_user_msgs + mid_user_evts + mid_queue_msgs
        )
        mid_all_present = all(p in mid_combined for p in prompts)

        # B2. The ordering of user messages in the mid-turn events
        #     log must match post order (seq asc = post order).
        mid_events_sorted = assertions.sort_by_seq(mid_events)
        mid_evts_in_order = _user_event_contents(mid_events_sorted)
        # Keep only unique preserving order for comparison.
        mid_evts_unique = _dedup_preserve_order(mid_evts_in_order)
        mid_evt_order_ok = mid_evts_unique == [
            p for p in prompts if p in mid_evts_unique
        ]

        # B3. After drain, history.messages must contain every prompt
        #     exactly once in post order.
        final_msgs_filter = [p for p in final_user_msgs if p in prompts]
        final_msg_order_ok = final_msgs_filter == prompts
        final_msg_count_ok = len(final_msgs_filter) == len(prompts)

        # B4. After drain, history.events user_message order must also
        #     match post order.
        final_evts_unique = _dedup_preserve_order(final_user_evts)
        final_evts_filter = [p for p in final_evts_unique if p in prompts]
        final_evt_order_ok = final_evts_filter == prompts

        # B5. Replay stream (joined with since=0) must see all user_messages.
        replay_evts_unique = _dedup_preserve_order(replay_user_evts)
        replay_all_present = all(p in replay_evts_unique for p in prompts)

        # B6. Basic sanity: msg1 was fast-path, msg2/msg3 queued.
        fp_ok = len(fp) == 1
        queued_ok = len(queued_ids) == 2

        # B7. Mid-turn /history must expose turn_active=true and the
        #     pending queue so the client sees the in-progress state.
        pending_msgs = [
            str(e.get("message", "")) for e in mid_pending_queue
        ]
        mid_pending_covers_queued = all(
            p in (pending_msgs + mid_user_msgs + mid_user_evts)
            for p in prompts[1:]
        )

        checks: list[tuple[str, tuple[bool, str]]] = [
            ("exactly 1 fast-path", (fp_ok, f"fp={fp}")),
            ("2 queued", (queued_ok, f"queued={queued_ids}")),
            (
                "mid-turn: turn_active flag true",
                (mid_turn_active, f"turn_active={mid_turn_active}"),
            ),
            (
                "mid-turn: pending_queue covers queued prompts",
                (
                    mid_pending_covers_queued,
                    f"pending_queue={pending_msgs}",
                ),
            ),
            (
                "mid-turn: all prompts visible in combined view",
                (
                    mid_all_present,
                    f"mid_messages={mid_user_msgs} "
                    f"mid_events_user={mid_user_evts} "
                    f"mid_queue={mid_queue_msgs}",
                ),
            ),
            (
                "mid-turn: user_message events in post order",
                (
                    mid_evt_order_ok,
                    f"mid_event_order={mid_evts_unique} expected={prompts}",
                ),
            ),
            (
                "final: history.messages contains every prompt",
                (
                    final_msg_count_ok,
                    f"final_messages_user={final_user_msgs}",
                ),
            ),
            (
                "final: history.messages in post order",
                (
                    final_msg_order_ok,
                    f"final_msgs_filter={final_msgs_filter} expected={prompts}",
                ),
            ),
            (
                "final: history.events user_message in post order",
                (
                    final_evt_order_ok,
                    f"final_evts_filter={final_evts_filter} expected={prompts}",
                ),
            ),
            (
                "replay stream: all prompts seen",
                (replay_all_present, f"replay_unique={replay_evts_unique}"),
            ),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, {
            "session": session.session_id,
            "correlation_ids": correlation_ids,
            "mid_messages_user": mid_user_msgs,
            "mid_events_user": mid_user_evts,
            "mid_queue_msgs": mid_queue_msgs,
            "final_messages_user": final_user_msgs,
            "final_events_user": final_user_evts,
            "replay_events_user": replay_user_evts,
        }
    finally:
        if stream is not None:
            try:
                stream.stop(timeout=2.0)
            except Exception:
                pass


if __name__ == "__main__":
    import json as _json
    import os as _os
    import sys

    app_id = sys.argv[1] if len(sys.argv) > 1 else "digitorn-chat"
    daemon_url = _os.environ.get("DAEMON_URL", "http://127.0.0.1:8000")
    email = _os.environ.get("DEV_EMAIL", "dev@digitorn.local")
    password = _os.environ.get("DEV_PASSWORD", "DevPassword123!")
    client = DevClient.with_user(
        email, password, daemon_url=daemon_url, register_if_missing=True,
    )
    t0 = time.time()
    ok, detail, art = scenario_midturn_rejoin(client, app_id)
    dur = time.time() - t0
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] scenario_midturn_rejoin ({dur:.1f}s)")
    print(detail)
    print()
    print("artifacts:")
    print(_json.dumps(art, indent=2, ensure_ascii=False))
    sys.exit(0 if ok else 1)
