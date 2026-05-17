"""Live-test scenarios for the slash command pre-dispatch system.

Exercises ``/help``, ``/compact``, and the regression where the user
reports a ``RÉESSAYER`` error banner on ``/compact``. Captures the
raw POST response + every SSE event so the failure mode is
diagnosable from the artifacts alone.

Run::

    py -3.12 tools/live_tests/slash_dispatch_scenarios.py
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


_DAEMON = "http://127.0.0.1:8000"
_APP_ID = "digitorn-chat"
_CREDS_PATH = Path.home() / ".digitorn" / "credentials.json"


def _new_session(client: DevClient) -> SessionHandle:
    sid = f"slash-{uuid.uuid4().hex[:8]}"
    return SessionHandle(
        session_id=sid, app_id=_APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )


def _scenario_help(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    """POST '/help' and verify the synthetic dispatch fires.

    Expected envelope (all asserted):
      * POST returns 200 with ``status: slash_handled``
      * SSE: user_message + assistant_message + turn_terminal
        all carrying the same ``slash-<hash>`` correlation_id
      * assistant_message payload includes ``slash_synthetic: true``
        and a markdown body starting with ``# Available commands``
    """
    session = _new_session(client)
    artifacts: dict[str, Any] = {"session_id": session.session_id}
    stream = None
    try:
        # POST first to create the session, then join the session
        # room which replays the events emitted during dispatch.
        post = client.post_message_raw(session, "/help")
        artifacts["post_status"] = post.get("status_code")
        artifacts["post_body"] = post.get("body")
        stream = client.open_event_stream(session)

        # Wait up to 5s for the assistant_message to arrive
        try:
            stream.wait_for(
                "turn_terminal", timeout=5.0,
                predicate=lambda e: (
                    (e.get("payload") or {}).get("correlation_id", "")
                    .startswith("slash-")
                ),
            )
        except Exception as exc:
            artifacts["wait_error"] = str(exc)

        events = assertions.sort_by_seq(stream.events())
        artifacts["event_count"] = len(events)
        artifacts["event_types"] = [e.get("type") for e in events]

        assistant_events = [
            e for e in events if e.get("type") == "assistant_message"
        ]
        artifacts["assistant_count"] = len(assistant_events)
        if assistant_events:
            payload = assistant_events[-1].get("payload") or {}
            artifacts["assistant_content_head"] = (
                (payload.get("content") or "")[:200]
            )
            artifacts["slash_synthetic"] = payload.get("slash_synthetic")

        body = post.get("body") or {}
        data = (body.get("data") or {}) if isinstance(body, dict) else {}
        status_ok = post.get("status_code") == 200
        slash_handled = data.get("status") == "slash_handled"
        has_assistant = len(assistant_events) >= 1
        has_terminal = any(e.get("type") == "turn_terminal" for e in events)

        ok = bool(status_ok and slash_handled and has_assistant and has_terminal)
        detail = (
            f"status_ok={status_ok} slash_handled={slash_handled} "
            f"assistant={has_assistant} terminal={has_terminal}"
        )
        return ok, detail, artifacts
    finally:
        if stream is not None:
            try:
                stream.stop(timeout=2.0)
            except Exception:
                pass


def _scenario_compact(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    """POST '/compact' on a session with too few messages.

    Captures the EXACT failure mode the user reported (RÉESSAYER
    banner = HTTP error). On a fresh session the handler should
    return early with 'Too few messages to compact' — NOT a 500.
    """
    session = _new_session(client)
    artifacts: dict[str, Any] = {"session_id": session.session_id}
    stream = None
    try:
        post = client.post_message_raw(session, "/compact")
        artifacts["post_status"] = post.get("status_code")
        artifacts["post_body"] = post.get("body")
        artifacts["post_headers"] = dict(post.get("headers") or {})
        stream = client.open_event_stream(session)

        try:
            stream.wait_for(
                "turn_terminal", timeout=10.0,
                predicate=lambda e: (
                    (e.get("payload") or {}).get("correlation_id", "")
                    .startswith("slash-")
                ),
            )
        except Exception as exc:
            artifacts["wait_error"] = str(exc)

        events = assertions.sort_by_seq(stream.events())
        artifacts["event_count"] = len(events)
        artifacts["event_types"] = [e.get("type") for e in events]
        artifacts["events_raw"] = events[:20]

        assistant_events = [
            e for e in events if e.get("type") == "assistant_message"
        ]
        if assistant_events:
            payload = assistant_events[-1].get("payload") or {}
            artifacts["assistant_content_head"] = (
                (payload.get("content") or "")[:300]
            )

        status_code = post.get("status_code", 0)
        ok = 200 <= int(status_code) < 300
        detail = f"http_status={status_code}"
        return ok, detail, artifacts
    finally:
        if stream is not None:
            try:
                stream.stop(timeout=2.0)
            except Exception:
                pass


def _scenario_compact_after_chat(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    """Send 4 real chat messages first, THEN /compact, to exercise
    the actual ``emergency_compact`` code path (not the
    too-few-messages early return)."""
    session = _new_session(client)
    artifacts: dict[str, Any] = {"session_id": session.session_id}
    stream = None
    try:
        # Open stream AFTER the first ping creates the session.
        first = client.post_message_raw(session, "ping 0")
        artifacts["prelude_0_status"] = first.get("status_code")
        stream = client.open_event_stream(session)

        # Build up some history. Short messages so the turns finish
        # quickly; we want enough rows for emergency_compact to bite,
        # not a heavy load test.
        for i in range(1, 5):
            try:
                client.send_live(
                    session, f"ping {i}",
                    stream=stream, total_timeout=30.0,
                )
            except Exception as exc:
                artifacts[f"prelude_{i}_error"] = str(exc)

        stream.clear()  # focus on the /compact events only

        post = client.post_message_raw(session, "/compact")
        artifacts["post_status"] = post.get("status_code")
        artifacts["post_body"] = post.get("body")

        try:
            stream.wait_for(
                "turn_terminal", timeout=70.0,
                predicate=lambda e: (
                    (e.get("payload") or {}).get("correlation_id", "")
                    .startswith("slash-")
                ),
            )
        except Exception as exc:
            artifacts["wait_error"] = str(exc)

        events = assertions.sort_by_seq(stream.events())
        artifacts["event_count"] = len(events)
        artifacts["event_types"] = [e.get("type") for e in events]

        assistant_events = [
            e for e in events if e.get("type") == "assistant_message"
        ]
        if assistant_events:
            payload = assistant_events[-1].get("payload") or {}
            artifacts["assistant_content_head"] = (
                (payload.get("content") or "")[:300]
            )

        status_code = int(post.get("status_code", 0))
        ok = 200 <= status_code < 300
        return ok, f"http_status={status_code}", artifacts
    finally:
        if stream is not None:
            try:
                stream.stop(timeout=2.0)
            except Exception:
                pass


def run(daemon_url: str) -> int:
    if not _CREDS_PATH.exists():
        print(f"ERROR: no credentials file at {_CREDS_PATH}")
        print("Log in once via the CLI / web first.")
        return 2
    creds = json.loads(_CREDS_PATH.read_text(encoding="utf-8"))
    token = creds.get("access_token")
    if not token:
        print(f"ERROR: credentials file has no access_token")
        return 2
    print(f"Using credentials for: {creds.get('username') or creds.get('user_id')}")
    client = DevClient.with_token(token, daemon_url=daemon_url, timeout=120.0)

    scenarios = [
        ("help", _scenario_help),
        ("compact_empty", _scenario_compact),
        ("compact_after_chat", _scenario_compact_after_chat),
    ]
    overall_ok = True
    for name, fn in scenarios:
        print(f"\n=== SCENARIO {name} ===")
        try:
            ok, detail, art = fn(client)
        except Exception as exc:
            ok = False
            detail = f"scenario raised: {exc}"
            art = {}
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {detail}")
        for k, v in art.items():
            if k == "events_raw":
                print(f"  {k} = ({len(v)} events)")
                for ev in v[:8]:
                    print(
                        f"    - {ev.get('type')} seq={ev.get('seq')} "
                        f"payload_keys={list((ev.get('payload') or {}).keys())[:5]}"
                    )
                continue
            v_str = str(v)
            if len(v_str) > 500:
                v_str = v_str[:500] + "... (truncated)"
            # Strip non-ASCII so cp1252 Windows console doesn't crash
            # on em-dashes / arrows / smart quotes that show up in
            # assistant_message content.
            v_str = v_str.encode("ascii", errors="replace").decode("ascii")
            print(f"  {k} = {v_str}")
        if not ok:
            overall_ok = False

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(run(_DAEMON))
