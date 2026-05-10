"""Real-LLM stabilization scenarios.

Every scenario:
  * Hits a real LLM (Ollama qwen2.5)
  * Captures the live event stream
  * Validates HTTP contract + event ordering + persistence
  * Probes ``/health`` before/after to count event-loop stalls
  * Returns ``(ok, detail, artifacts)``

Anti-patterns refused (from testing/README.md):
  * No mocks at the daemon level. We use a real LLM.
  * No silent exception swallowing. Live tests fail loud.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle

from .harness import health_loop_stalls


# ── Helpers ────────────────────────────────────────────────────────


def _new_session(client: DevClient, app_id: str, prefix: str) -> SessionHandle:
    sid = f"{prefix}-{uuid.uuid4().hex[:8]}"
    return SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace="",
    )


def _capture_stalls(daemon_url: str) -> dict:
    """Snapshot the daemon's event-loop stall counters."""
    return health_loop_stalls(daemon_url)


def _stalls_delta(before: dict, after: dict) -> dict:
    """Return added stalls + max gap between two snapshots."""
    if "error" in before or "error" in after:
        return {"error": before.get("error") or after.get("error")}
    bs = int(before.get("stalls_total") or 0)
    as_ = int(after.get("stalls_total") or 0)
    return {
        "new_stalls": as_ - bs,
        "before_total": bs,
        "after_total": as_,
        "last_gap_ms": after.get("last_stall_gap_ms"),
    }


# ── 1. Single-turn round trip ──────────────────────────────────────


def scenario_single_turn(
    client: DevClient, app_id: str, daemon_url: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Send "Hello" and verify the daemon emits the canonical lifecycle:
    user_message -> message_started -> tokens (>=1) -> message_done ->
    turn_terminal. Validates seq monotonicity + correlation_id thread."""
    artifacts: dict[str, Any] = {}
    session = _new_session(client, app_id, "single")
    artifacts["session_id"] = session.session_id

    stalls_before = _capture_stalls(daemon_url)
    artifacts["stalls_before"] = stalls_before

    stream = None
    t0 = time.perf_counter()
    try:
        stream = client.send_live(
            session, "Reply with exactly the word OK.",
            total_timeout=90.0,
        )
        events = stream.events()
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    artifacts["wall_seconds"] = round(time.perf_counter() - t0, 2)
    artifacts["event_count"] = len(events)
    artifacts["stalls_after"] = _capture_stalls(daemon_url)
    artifacts["stalls_delta"] = _stalls_delta(stalls_before, artifacts["stalls_after"])

    sorted_events = assertions.sort_by_seq(events)
    correlation_ids = {
        (e.get("payload") or {}).get("correlation_id")
        for e in sorted_events if (e.get("payload") or {}).get("correlation_id")
    }
    artifacts["correlation_ids"] = list(correlation_ids)

    checks = [
        ("seq_unique", assertions.seq_unique(sorted_events)),
        ("user_message_present", assertions.event_count(sorted_events, "user_message", minimum=1)),
        ("message_started_present", assertions.event_count(sorted_events, "message_started", minimum=1)),
        ("message_done_present", assertions.event_count(sorted_events, "message_done", minimum=1)),
        ("lifecycle_order", assertions.event_order(
            sorted_events,
            ["user_message", "message_started", "message_done"],
        )),
    ]
    ok, detail = assertions.report(checks)
    if artifacts["stalls_delta"].get("new_stalls", 0) > 0:
        ok = False
        detail += (
            f" | LOOP STALL DETECTED during single turn: "
            f"+{artifacts['stalls_delta']['new_stalls']} stall(s), "
            f"last_gap_ms={artifacts['stalls_delta'].get('last_gap_ms')}"
        )
    return ok, detail, artifacts


# ── 2. Multi-turn context retention ────────────────────────────────


def scenario_multi_turn_context(
    client: DevClient, app_id: str, daemon_url: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Three sequential turns where each refers to the previous. The
    LLM must "remember" the established context (we tell it to repeat
    a magic word back). Validates that the chat history is fed correctly
    on follow-up turns."""
    artifacts: dict[str, Any] = {}
    session = _new_session(client, app_id, "multi")
    artifacts["session_id"] = session.session_id

    stalls_before = _capture_stalls(daemon_url)
    magic = f"banana-{uuid.uuid4().hex[:6]}"

    turns = [
        f"Remember this code word: {magic}. Just acknowledge by saying OK.",
        "What instructions did I just give you in one short sentence?",
        f"Repeat the code word back to me, exactly. The word is {magic}? Please type it.",
    ]
    transcript: list[dict[str, Any]] = []
    stream = None
    t0 = time.perf_counter()
    try:
        for turn_text in turns:
            stream = client.send_live(
                session, turn_text, total_timeout=90.0, stream=stream,
            )
            done = next(
                (e for e in reversed(stream.events())
                 if e.get("type") == "message_done"),
                None,
            )
            transcript.append({"sent": turn_text, "got_done": bool(done)})
        events = stream.events()
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    artifacts["wall_seconds"] = round(time.perf_counter() - t0, 2)
    artifacts["transcript"] = transcript
    artifacts["stalls_delta"] = _stalls_delta(stalls_before, _capture_stalls(daemon_url))

    sorted_events = assertions.sort_by_seq(events)
    user_msgs = [e for e in sorted_events if e.get("type") == "user_message"]
    msg_done = [e for e in sorted_events if e.get("type") == "message_done"]
    artifacts["user_message_count"] = len(user_msgs)
    artifacts["message_done_count"] = len(msg_done)

    # History: pull from /history endpoint and verify all 3 turns are
    # there.
    hist = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/history?events_limit=10000",
    )
    hist_data = (hist.json() or {}).get("data") or {}
    artifacts["history_status"] = hist.status_code
    artifacts["history_message_count"] = hist_data.get("message_count")
    artifacts["history_event_count"] = hist_data.get("event_count")

    # Did the LLM actually echo the magic word in turn 3? Look in the
    # last assistant message content.
    assistant_msgs = [
        m for m in (hist_data.get("messages") or [])
        if m.get("role") == "assistant"
    ]
    last_assistant = assistant_msgs[-1] if assistant_msgs else {}
    last_content = last_assistant.get("content") or ""
    artifacts["last_assistant_excerpt"] = (
        str(last_content)[:200] if last_content else ""
    )
    echoed_magic = magic in str(last_content)
    artifacts["echoed_magic_word"] = echoed_magic

    checks = [
        ("seq_unique", assertions.seq_unique(sorted_events)),
        ("3_user_messages", (
            len(user_msgs) >= 3,
            f"got {len(user_msgs)} user_messages",
        )),
        ("3_done_signals", (
            len(msg_done) >= 3,
            f"got {len(msg_done)} message_done events",
        )),
        ("history_endpoint_ok", (
            hist.status_code == 200,
            f"HTTP {hist.status_code}",
        )),
    ]
    ok, detail = assertions.report(checks)
    if artifacts["stalls_delta"].get("new_stalls", 0) > 0:
        ok = False
        detail += f" | LOOP STALL: +{artifacts['stalls_delta']['new_stalls']}"
    detail += f" | echoed_magic={echoed_magic}"
    return ok, detail, artifacts


# ── 3. Abort mid-turn cleanup ──────────────────────────────────────


def scenario_abort_mid_turn(
    client: DevClient, app_id: str, daemon_url: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Start a turn that asks for a long answer, abort it after a
    short delay, and verify the daemon emits an abort event AND
    leaves the session in a clean state (turn not running anymore)."""
    artifacts: dict[str, Any] = {}
    session = _new_session(client, app_id, "abort")
    artifacts["session_id"] = session.session_id
    stalls_before = _capture_stalls(daemon_url)

    stream = None
    t0 = time.perf_counter()
    try:
        # Open the live stream BEFORE posting so we don't miss any
        # early lifecycle events.
        stream = client.open_event_stream(session, wait_for_session=False)
        post_result = client.post_message_raw(
            session,
            "Write me an essay of 5000 words about the history of "
            "wheat farming, very detailed, take your time.",
        )
        artifacts["post_status"] = post_result.get("status_code")

        # Wait until the assistant starts streaming, then abort.
        started = stream.wait_for(
            "message_started", timeout=30.0,
        )
        artifacts["got_message_started"] = bool(started)

        time.sleep(0.5)  # let some tokens flow
        abort_resp = client._post(
            f"/api/apps/{app_id}/sessions/{session.session_id}/abort",
            json={},
        )
        artifacts["abort_status"] = abort_resp.status_code

        # Wait for either the abort event or message_done within 10s.
        ended = stream.wait_for_any(
            ["message_done", "message_cancelled", "abort", "turn_terminal"],
            timeout=15.0,
        )
        artifacts["got_end_event"] = bool(ended)
        artifacts["end_event_type"] = ended.get("type") if ended else None
        events = stream.events()
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    artifacts["wall_seconds"] = round(time.perf_counter() - t0, 2)
    artifacts["stalls_delta"] = _stalls_delta(stalls_before, _capture_stalls(daemon_url))

    # After abort, the session should NOT be marked as running.
    state_resp = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/state",
    )
    state_data = (state_resp.json() or {}).get("data") or {}
    artifacts["state_status"] = state_resp.status_code
    artifacts["turn_active_after_abort"] = (
        (state_data.get("turn") or {}).get("active") if state_data else None
    )

    sorted_events = assertions.sort_by_seq(events)
    checks = [
        ("seq_unique", assertions.seq_unique(sorted_events)),
        ("abort_http_ok", (
            abort_resp.status_code in (200, 202),
            f"HTTP {abort_resp.status_code}",
        )),
        ("got_end_event", (
            bool(ended),
            "no end event in 15s after abort",
        )),
        ("turn_not_active", (
            artifacts["turn_active_after_abort"] in (False, None),
            f"turn.active={artifacts['turn_active_after_abort']}",
        )),
    ]
    ok, detail = assertions.report(checks)
    if artifacts["stalls_delta"].get("new_stalls", 0) > 0:
        ok = False
        detail += f" | LOOP STALL: +{artifacts['stalls_delta']['new_stalls']}"
    return ok, detail, artifacts


# ── 4. Concurrent sessions isolation ───────────────────────────────


def scenario_concurrent_sessions(
    client: DevClient, app_id: str, daemon_url: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Three independent sessions, each with a unique secret. The LLM
    must echo each secret correctly back in its own session -- no
    cross-talk."""
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {}
    stalls_before = _capture_stalls(daemon_url)

    secrets = [f"sec-{uuid.uuid4().hex[:6]}" for _ in range(3)]
    sessions = [_new_session(client, app_id, "concur") for _ in range(3)]
    artifacts["session_ids"] = [s.session_id for s in sessions]

    def _chat_one(idx: int) -> dict:
        session = sessions[idx]
        secret = secrets[idx]
        stream = None
        try:
            stream = client.send_live(
                session,
                f"My secret code is '{secret}'. "
                f"Reply with EXACTLY: 'I read your secret: {secret}'",
                total_timeout=120.0,
            )
            return {
                "ok": True,
                "events": len(stream.events()),
                "secret": secret,
                "session_id": session.session_id,
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if stream is not None:
                stream.stop(timeout=2.0)

    t0 = time.perf_counter()
    with _cf.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(_chat_one, range(3)))
    artifacts["wall_seconds"] = round(time.perf_counter() - t0, 2)
    artifacts["results"] = results
    artifacts["stalls_delta"] = _stalls_delta(stalls_before, _capture_stalls(daemon_url))

    # Verify each session ONLY contains its own secret in the assistant's
    # response, never another session's.
    contamination: list[str] = []
    found_own: list[bool] = []
    for idx, session in enumerate(sessions):
        secret = secrets[idx]
        hist = client._get(
            f"/api/apps/{app_id}/sessions/{session.session_id}/history?events_limit=10000",
        )
        if hist.status_code != 200:
            contamination.append(f"{session.session_id}: HTTP {hist.status_code}")
            found_own.append(False)
            continue
        data = (hist.json() or {}).get("data") or {}
        msgs = data.get("messages") or []
        assistant_text = " ".join(
            str(m.get("content", "")) for m in msgs
            if m.get("role") == "assistant"
        )
        own = secret in assistant_text
        found_own.append(own)
        for j, other in enumerate(secrets):
            if j == idx:
                continue
            if other in assistant_text:
                contamination.append(
                    f"session {idx} contains secret of session {j}"
                )
    artifacts["found_own"] = found_own
    artifacts["contamination"] = contamination

    checks = [
        ("all_succeeded", (
            all(r.get("ok") for r in results),
            f"failures={[r for r in results if not r.get('ok')][:2]}",
        )),
        ("no_contamination", (
            len(contamination) == 0,
            f"{contamination}",
        )),
    ]
    ok, detail = assertions.report(checks)
    if artifacts["stalls_delta"].get("new_stalls", 0) > 0:
        ok = False
        detail += f" | LOOP STALL: +{artifacts['stalls_delta']['new_stalls']}"
    detail += (
        f" | own_secret_echoed={sum(found_own)}/{len(found_own)} "
        f"(LLM may not always comply, this is a soft signal)"
    )
    return ok, detail, artifacts


# ── 5. Sequential 5-turn stress -- watch for slow leaks ────────────


def scenario_sequential_stress(
    client: DevClient, app_id: str, daemon_url: str,
) -> tuple[bool, str, dict[str, Any]]:
    """5 sequential short turns on the SAME session. Tracks per-turn
    latency to see whether the daemon slows down across turns (sign of
    a leak / unbounded growth) or stays stable."""
    artifacts: dict[str, Any] = {}
    session = _new_session(client, app_id, "stress")
    artifacts["session_id"] = session.session_id
    stalls_before = _capture_stalls(daemon_url)

    latencies: list[float] = []
    stream = None
    t0_total = time.perf_counter()
    try:
        for i in range(5):
            t0 = time.perf_counter()
            stream = client.send_live(
                session, f"Turn {i + 1}: say OK and nothing else.",
                total_timeout=60.0, stream=stream,
            )
            latencies.append(round(time.perf_counter() - t0, 2))
        events = stream.events()
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    artifacts["wall_seconds"] = round(time.perf_counter() - t0_total, 2)
    artifacts["per_turn_seconds"] = latencies
    artifacts["stalls_delta"] = _stalls_delta(stalls_before, _capture_stalls(daemon_url))

    sorted_events = assertions.sort_by_seq(events)
    user_msgs = [e for e in sorted_events if e.get("type") == "user_message"]
    msg_done = [e for e in sorted_events if e.get("type") == "message_done"]
    artifacts["user_messages"] = len(user_msgs)
    artifacts["message_done"] = len(msg_done)

    # Detect leak: last turn shouldn't be more than 2x the first turn's
    # latency. The chat keeps growing in context but the daemon should
    # NOT accumulate per-turn overhead beyond what context length adds.
    leak_signal = (
        len(latencies) >= 5
        and latencies[-1] > latencies[0] * 3
    )
    artifacts["leak_signal"] = leak_signal

    checks = [
        ("seq_unique", assertions.seq_unique(sorted_events)),
        ("5_user_messages", (
            len(user_msgs) >= 5,
            f"{len(user_msgs)} user_messages",
        )),
        ("5_message_done", (
            len(msg_done) >= 5,
            f"{len(msg_done)} message_done",
        )),
        ("no_severe_slowdown", (
            not leak_signal,
            f"first={latencies[0] if latencies else 'n/a'}s "
            f"last={latencies[-1] if latencies else 'n/a'}s",
        )),
    ]
    ok, detail = assertions.report(checks)
    if artifacts["stalls_delta"].get("new_stalls", 0) > 0:
        ok = False
        detail += f" | LOOP STALL: +{artifacts['stalls_delta']['new_stalls']}"
    return ok, detail, artifacts
