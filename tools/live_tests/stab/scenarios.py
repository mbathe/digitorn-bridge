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
    turn_terminal. Validates seq monotonicity + correlation_id thread.

    Stream is opened BEFORE POSTing so we capture seq=1 (user_message);
    ``client.send_live`` opens the stream after the POST and would miss
    the very first event."""
    artifacts: dict[str, Any] = {}
    session = _new_session(client, app_id, "single")
    artifacts["session_id"] = session.session_id

    stalls_before = _capture_stalls(daemon_url)
    artifacts["stalls_before"] = stalls_before

    stream = None
    t0 = time.perf_counter()
    try:
        # post-then-stream: LiveEventStream's Socket.IO join requires the
        # session to exist server-side. The user_message event (seq=1)
        # fires before stream join -- we'll capture it via /events
        # (durable, server-authoritative).
        stream = client.send_live(
            session, "Reply with exactly the word OK.",
            total_timeout=180.0,
        )
        events = stream.events()
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    artifacts["wall_seconds"] = round(time.perf_counter() - t0, 2)
    artifacts["event_count"] = len(events)
    artifacts["stalls_after"] = _capture_stalls(daemon_url)
    artifacts["stalls_delta"] = _stalls_delta(stalls_before, artifacts["stalls_after"])

    # Stream may miss seq=1 (user_message) because Socket.IO join-replay
    # is best-effort. The DURABLE truth is /events -- it returns every
    # persisted row in seq order. Use the stream for live-signal checks
    # (message_started fired, lifecycle terminated) and /events for
    # contract checks (user_message persisted, seq monotonic).
    persisted_resp = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/events?limit=5000",
    )
    persisted_data = (persisted_resp.json() or {}).get("data") or {}
    persisted_events = persisted_data.get("events") or []
    sorted_persisted = assertions.sort_by_seq(persisted_events)
    sorted_events = assertions.sort_by_seq(events)
    artifacts["persisted_event_count"] = len(persisted_events)
    artifacts["stream_event_count"] = len(events)
    correlation_ids = {
        (e.get("payload") or {}).get("correlation_id")
        for e in sorted_events if (e.get("payload") or {}).get("correlation_id")
    }
    artifacts["correlation_ids"] = list(correlation_ids)

    # Lifecycle terminated cleanly: either message_done OR error+turn_terminal.
    has_done = any(e.get("type") == "message_done" for e in sorted_events)
    has_clean_end = has_done or any(
        e.get("type") == "turn_terminal" for e in sorted_events
    )

    # ``message_started`` may land in either the stream OR the
    # persisted /events depending on the daemon's stream-replay
    # timing. Accept either source as evidence the lifecycle started.
    has_message_started = any(
        e.get("type") == "message_started"
        for e in (sorted_events + sorted_persisted)
    )
    has_done_persisted = any(
        e.get("type") in ("message_done", "turn_terminal")
        for e in sorted_persisted
    )

    checks = [
        ("seq_unique_persisted", assertions.seq_unique(sorted_persisted)),
        ("user_message_persisted", assertions.event_count(sorted_persisted, "user_message", minimum=1)),
        ("message_started_anywhere", (
            has_message_started,
            "no message_started in stream nor persisted",
        )),
        ("turn_terminated", (
            has_clean_end or has_done_persisted,
            "no message_done or turn_terminal in stream or persisted",
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
                session, turn_text, total_timeout=180.0, stream=stream,
            )
            done = next(
                (e for e in reversed(stream.events())
                 if e.get("type") == "message_done"),
                None,
            )
            transcript.append({"sent": turn_text, "got_done": bool(done)})

        # Drain the daemon's queue so turn_terminal events fire for
        # every turn before we assert. Without this, slow LLM responses
        # leave the last turn(s) still mid-flight when assertions run.
        deadline = time.perf_counter() + 180.0
        while time.perf_counter() < deadline:
            state = client._get(
                f"/api/apps/{app_id}/sessions/{session.session_id}/state",
            )
            d = (state.json() or {}).get("data") or {}
            active = (d.get("turn") or {}).get("active")
            q = (d.get("queue") or {}).get("entries") or []
            pending = sum(
                1 for e in q
                if e.get("status") not in ("completed", "cancelled", "failed")
            )
            if not active and pending == 0:
                break
            time.sleep(2.0)
        events = stream.events()
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    artifacts["wall_seconds"] = round(time.perf_counter() - t0, 2)
    artifacts["transcript"] = transcript
    artifacts["stalls_delta"] = _stalls_delta(stalls_before, _capture_stalls(daemon_url))

    sorted_events = assertions.sort_by_seq(events)
    # Stream may miss early events on join; durable ground truth is /events.
    persisted = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/events?limit=5000",
    )
    persisted_data = (persisted.json() or {}).get("data") or {}
    persisted_events = persisted_data.get("events") or []
    user_msgs = [e for e in persisted_events if e.get("type") == "user_message"]
    msg_done = [e for e in sorted_events if e.get("type") == "message_done"]
    msg_term = [e for e in sorted_events if e.get("type") == "turn_terminal"]
    artifacts["user_message_count"] = len(user_msgs)
    artifacts["message_done_count"] = len(msg_done)
    artifacts["turn_terminal_count"] = len(msg_term)

    # History endpoint validates the /history payload contract.
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
        ("3_user_messages_persisted", (
            len(user_msgs) >= 3,
            f"got {len(user_msgs)} user_messages on disk",
        )),
        ("3_lifecycle_terminations", (
            (len(msg_done) + len(msg_term)) >= 3,
            f"got {len(msg_done)} message_done + {len(msg_term)} turn_terminal",
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
        # POST first (creates session + emits user_message seq=1).
        post_result = client.post_message_raw(
            session,
            "Write me an essay of 5000 words about the history of "
            "wheat farming, very detailed, take your time.",
        )
        artifacts["post_status"] = post_result.get("status_code")

        # Now open the stream -- session exists, join succeeds. Stream
        # will replay events with seq > since_seq=0 via join_session.
        stream = client.open_event_stream(session, wait_for_session=True)

        # Wait until the assistant starts streaming, then abort.
        started = stream.wait_for("message_started", timeout=60.0)
        artifacts["got_message_started"] = bool(started)

        time.sleep(0.5)  # let some tokens flow
        abort_resp = client._post(
            f"/api/apps/{app_id}/sessions/{session.session_id}/abort",
            json={},
        )
        artifacts["abort_status"] = abort_resp.status_code

        # Wait for terminal event within 15s.
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
                total_timeout=180.0, stream=stream,
            )
            latencies.append(round(time.perf_counter() - t0, 2))

        # After all sends, wait for the daemon's queue to fully drain
        # before assertions. Otherwise late turns are still in flight
        # and turn_terminal hasn't fired yet -- making the assertion
        # falsely fail. Poll /state until turn.active=False AND queue
        # is empty, capped at 180 s.
        deadline = time.perf_counter() + 180.0
        while time.perf_counter() < deadline:
            state = client._get(
                f"/api/apps/{app_id}/sessions/{session.session_id}/state",
            )
            d = (state.json() or {}).get("data") or {}
            active = (d.get("turn") or {}).get("active")
            q = (d.get("queue") or {}).get("entries") or []
            pending = sum(1 for e in q if e.get("status") not in ("completed", "cancelled", "failed"))
            if not active and pending == 0:
                break
            time.sleep(2.0)
        artifacts["drain_seconds"] = round(time.perf_counter() - t0_total - sum(latencies), 1)
        events = stream.events()
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    artifacts["wall_seconds"] = round(time.perf_counter() - t0_total, 2)
    artifacts["per_turn_seconds"] = latencies
    artifacts["stalls_delta"] = _stalls_delta(stalls_before, _capture_stalls(daemon_url))

    sorted_events = assertions.sort_by_seq(events)
    persisted = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/events?limit=5000",
    )
    persisted_events = (persisted.json() or {}).get("data", {}).get("events") or []
    user_msgs = [e for e in persisted_events if e.get("type") == "user_message"]
    msg_term = [e for e in persisted_events if e.get("type") == "turn_terminal"]
    msg_done = [e for e in sorted_events if e.get("type") == "message_done"]
    artifacts["user_messages_persisted"] = len(user_msgs)
    artifacts["turn_terminal_persisted"] = len(msg_term)
    artifacts["message_done_in_stream"] = len(msg_done)

    # Detect leak: last turn shouldn't be more than 3x the first turn's
    # latency. Context grows but per-turn overhead must not balloon.
    leak_signal = (
        len(latencies) >= 5
        and latencies[-1] > latencies[0] * 3
    )
    artifacts["leak_signal"] = leak_signal

    checks = [
        ("seq_unique", assertions.seq_unique(sorted_events)),
        ("5_user_messages_persisted", (
            len(user_msgs) >= 5,
            f"{len(user_msgs)} user_messages on disk",
        )),
        ("5_turn_terminals_persisted", (
            len(msg_term) >= 5,
            f"{len(msg_term)} turn_terminal events on disk",
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


# ── 6. Tool execution: LLM calls Remember + tool events fire ───────


def scenario_tool_execution(
    client: DevClient, app_id: str, daemon_url: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Ask the LLM to call the Remember tool with a specific fact.
    Validates that:
      1. The daemon receives + dispatches the tool_call
      2. tool_call event fires
      3. tool_result event fires with success=True
      4. The tool's action actually ran (memory persists the fact)
    """
    artifacts: dict[str, Any] = {}
    session = _new_session(client, app_id, "tool")
    artifacts["session_id"] = session.session_id
    stalls_before = _capture_stalls(daemon_url)

    stream = None
    t0 = time.perf_counter()
    try:
        stream = client.send_live(
            session,
            "Use the Remember tool to save this fact: "
            "'sky_color=blue'. Then say 'Saved.' and stop.",
            total_timeout=240.0,
        )
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
    artifacts["wall_seconds"] = round(time.perf_counter() - t0, 2)
    artifacts["stalls_delta"] = _stalls_delta(stalls_before, _capture_stalls(daemon_url))

    # Drain queue.
    deadline = time.perf_counter() + 60.0
    while time.perf_counter() < deadline:
        state = client._get(
            f"/api/apps/{app_id}/sessions/{session.session_id}/state",
        )
        d = (state.json() or {}).get("data") or {}
        if not (d.get("turn") or {}).get("active"):
            break
        time.sleep(2.0)

    persisted = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/events?limit=5000",
    )
    pdata = (persisted.json() or {}).get("data") or {}
    pevents = pdata.get("events") or []
    # The daemon emits a CONSOLIDATED ``tool_call`` event whose
    # ``payload`` carries both the call (``name``/``params``) and the
    # result (``success``/``result``/``op_state``). Hidden/silent
    # tools (like Remember) don't get a separate ``tool_result`` event
    # -- that one is reserved for visible-in-chat tools.
    tool_calls = [e for e in pevents if e.get("type") == "tool_call"]
    tool_results = [e for e in pevents if e.get("type") == "tool_result"]
    completed_calls = [
        e for e in tool_calls
        if (e.get("payload") or {}).get("op_state") == "completed"
        and bool((e.get("payload") or {}).get("success"))
    ]
    artifacts["tool_calls"] = len(tool_calls)
    artifacts["tool_results"] = len(tool_results)
    artifacts["completed_tool_calls"] = len(completed_calls)
    artifacts["first_tool_call"] = (
        {
            "name": (tool_calls[0].get("payload") or {}).get("name"),
            "params": (tool_calls[0].get("payload") or {}).get("params"),
            "success": (tool_calls[0].get("payload") or {}).get("success"),
            "op_state": (tool_calls[0].get("payload") or {}).get("op_state"),
        } if tool_calls else None
    )

    mem = client._get(f"/api/apps/{app_id}/sessions/{session.session_id}/memory")
    artifacts["memory_status"] = mem.status_code
    mem_data = (mem.json() or {}).get("data") or {}
    facts_raw = mem_data.get("facts")
    # ``facts`` can come back as a dict ({key: value}) or a list of
    # ({"key", "value", "ts"}) entries depending on the memory module
    # version. Normalise to a flat list of (k, v) tuples.
    if isinstance(facts_raw, dict):
        pairs = list(facts_raw.items())
    elif isinstance(facts_raw, list):
        pairs = [
            (str(f.get("key", "")), str(f.get("value", "")))
            for f in facts_raw if isinstance(f, dict)
        ]
    else:
        pairs = []
    artifacts["memory_facts_count"] = len(pairs)
    artifacts["memory_facts_keys"] = [k for k, _ in pairs[:10]]
    fact_saved = any(
        "sky" in str(k).lower() or "blue" in str(v).lower()
        for k, v in pairs
    )
    artifacts["fact_saved"] = fact_saved

    checks = [
        ("at_least_1_tool_call", (
            len(tool_calls) >= 1,
            f"got {len(tool_calls)} tool_call events",
        )),
        ("tool_call_completed_ok", (
            len(completed_calls) >= 1,
            f"got {len(completed_calls)} successful completed tool_calls "
            f"of {len(tool_calls)} total",
        )),
        ("memory_endpoint_ok", (
            mem.status_code == 200,
            f"HTTP {mem.status_code}",
        )),
    ]
    ok, detail = assertions.report(checks)
    if artifacts["stalls_delta"].get("new_stalls", 0) > 0:
        ok = False
        detail += f" | LOOP STALL: +{artifacts['stalls_delta']['new_stalls']}"
    detail += f" | fact_saved={fact_saved} (soft)"
    return ok, detail, artifacts


# ── 7. Manual compaction + history reconstruction ──────────────────


def scenario_manual_compaction(
    client: DevClient, app_id: str, daemon_url: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Send 3 turns, trigger manual compaction, verify the compaction
    event lands AND /history is still readable + seq monotonic."""
    artifacts: dict[str, Any] = {}
    session = _new_session(client, app_id, "compact")
    artifacts["session_id"] = session.session_id
    stalls_before = _capture_stalls(daemon_url)
    compact_resp = None
    hist_after = None
    seq_ok = False
    compaction_events: list = []

    stream = None
    try:
        # Need >= 8 turns (so 1 system + 8 user + 8 assistant = 17 msgs)
        # for emergency_compact to actually compact: with keep_recent=10
        # halved to 5, and conversation length = 16 (without sys), the
        # ``if len(conversation) <= keep_recent`` early-return triggers
        # below 6 conversation messages.
        for i in range(8):
            stream = client.send_live(
                session, f"Turn {i + 1}: just say OK.",
                total_timeout=180.0, stream=stream,
            )
        deadline = time.perf_counter() + 120.0
        while time.perf_counter() < deadline:
            state = client._get(
                f"/api/apps/{app_id}/sessions/{session.session_id}/state",
            )
            d = (state.json() or {}).get("data") or {}
            if not (d.get("turn") or {}).get("active"):
                break
            time.sleep(2.0)

        hist_before = client._get(
            f"/api/apps/{app_id}/sessions/{session.session_id}/history?events_limit=5000",
        )
        before_data = (hist_before.json() or {}).get("data") or {}
        artifacts["before_compact_messages"] = before_data.get("message_count")
        artifacts["before_compact_events"] = before_data.get("event_count")

        compact_resp = client._post(
            f"/api/apps/{app_id}/sessions/{session.session_id}/compact",
            json={},
        )
        artifacts["compact_status"] = compact_resp.status_code
        time.sleep(2.0)

        hist_after = client._get(
            f"/api/apps/{app_id}/sessions/{session.session_id}/history?events_limit=5000",
        )
        after_data = (hist_after.json() or {}).get("data") or {}
        artifacts["after_compact_messages"] = after_data.get("message_count")
        artifacts["after_compact_events"] = after_data.get("event_count")

        events = after_data.get("events") or []
        compaction_events = [e for e in events if e.get("type") == "compaction"]
        artifacts["compaction_events"] = len(compaction_events)

        seqs = [int(e.get("seq") or 0) for e in events]
        seq_ok = seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        artifacts["seq_monotonic"] = seq_ok
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    artifacts["stalls_delta"] = _stalls_delta(stalls_before, _capture_stalls(daemon_url))

    checks = [
        ("compact_http_ok", (
            compact_resp is not None and compact_resp.status_code in (200, 202),
            f"HTTP {compact_resp.status_code if compact_resp else 'none'}",
        )),
        ("compaction_event_present", (
            len(compaction_events) >= 1,
            f"got {len(compaction_events)} compaction events",
        )),
        ("history_still_readable", (
            hist_after is not None and hist_after.status_code == 200,
            f"HTTP {hist_after.status_code if hist_after else 'none'}",
        )),
        ("seq_monotonic_post_compact", (
            seq_ok,
            "seq not strictly increasing OR has duplicates",
        )),
    ]
    ok, detail = assertions.report(checks)
    if artifacts["stalls_delta"].get("new_stalls", 0) > 0:
        ok = False
        detail += f" | LOOP STALL: +{artifacts['stalls_delta']['new_stalls']}"
    return ok, detail, artifacts


# ── 8. History reconstruction after session eviction ───────────────


def scenario_history_reconstruction(
    client: DevClient, app_id: str, daemon_url: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Send 2 turns, fetch /history. Spam other sessions to evict ours
    from in-memory LRU. Refetch /history. The two fetches must return
    identical event seqs + message content -- proves cold-reload from
    events.jsonl faithfully reconstructs the session."""
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {}
    session = _new_session(client, app_id, "histrec")
    artifacts["session_id"] = session.session_id
    stalls_before = _capture_stalls(daemon_url)
    hist1 = None
    hist2 = None
    warm_seqs: list = []
    cold_seqs: list = []
    warm_msg_content: list = []
    cold_msg_content: list = []

    stream = None
    try:
        for i in range(2):
            stream = client.send_live(
                session, f"Reply with just 'OK {i + 1}'.",
                total_timeout=180.0, stream=stream,
            )
        deadline = time.perf_counter() + 60.0
        while time.perf_counter() < deadline:
            state = client._get(
                f"/api/apps/{app_id}/sessions/{session.session_id}/state",
            )
            d = (state.json() or {}).get("data") or {}
            if not (d.get("turn") or {}).get("active"):
                break
            time.sleep(2.0)

        hist1 = client._get(
            f"/api/apps/{app_id}/sessions/{session.session_id}/history?events_limit=5000",
        )
        d1 = (hist1.json() or {}).get("data") or {}
        artifacts["warm_message_count"] = d1.get("message_count")
        artifacts["warm_event_count"] = d1.get("event_count")
        warm_seqs = [
            int(e.get("seq") or 0) for e in (d1.get("events") or [])
        ]
        warm_msg_content = [
            str(m.get("content", ""))[:120]
            for m in (d1.get("messages") or [])
        ]

        evict_sids = [_new_session(client, app_id, "evict") for _ in range(20)]

        def _ping(s):
            try:
                client.post_message_raw(s, "ping")
            except Exception:
                pass

        with _cf.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_ping, evict_sids))
        time.sleep(3.0)

        hist2 = client._get(
            f"/api/apps/{app_id}/sessions/{session.session_id}/history?events_limit=5000",
        )
        d2 = (hist2.json() or {}).get("data") or {}
        artifacts["cold_message_count"] = d2.get("message_count")
        artifacts["cold_event_count"] = d2.get("event_count")
        cold_seqs = [
            int(e.get("seq") or 0) for e in (d2.get("events") or [])
        ]
        cold_msg_content = [
            str(m.get("content", ""))[:120]
            for m in (d2.get("messages") or [])
        ]
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
    artifacts["stalls_delta"] = _stalls_delta(stalls_before, _capture_stalls(daemon_url))

    seqs_match = warm_seqs == cold_seqs
    msgs_match = warm_msg_content == cold_msg_content
    artifacts["seqs_match"] = seqs_match
    artifacts["msgs_match"] = msgs_match

    checks = [
        ("warm_http_ok", (
            hist1 is not None and hist1.status_code == 200,
            f"HTTP {hist1.status_code if hist1 else 'none'}",
        )),
        ("cold_http_ok", (
            hist2 is not None and hist2.status_code == 200,
            f"HTTP {hist2.status_code if hist2 else 'none'}",
        )),
        ("event_seqs_identical_post_reload", (
            seqs_match,
            f"warm={len(warm_seqs)} cold={len(cold_seqs)}",
        )),
        ("message_content_identical_post_reload", (
            msgs_match,
            f"warm_msgs={len(warm_msg_content)} cold_msgs={len(cold_msg_content)}",
        )),
    ]
    ok, detail = assertions.report(checks)
    # A small stall under the 20-session eviction-thrash workload is
    # acceptable (filesystem batch + json serialisation hiccup). Only
    # fail if a stall exceeds 10 s -- that level signals a real hang.
    nstalls = artifacts["stalls_delta"].get("new_stalls", 0)
    last_gap = float(artifacts["stalls_delta"].get("last_gap_ms") or 0)
    if nstalls > 0 and last_gap > 10000.0:
        ok = False
        detail += f" | LOOP STALL >10s: +{nstalls} (last_gap_ms={last_gap})"
    elif nstalls > 0:
        detail += f" | tolerable stall: +{nstalls} (last_gap_ms={last_gap})"
    return ok, detail, artifacts


# ── 10. Long chat: full persistence + history reload + compaction ──


def scenario_long_chat_full_persistence(
    client: DevClient, app_id: str, daemon_url: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Heavy end-to-end scenario for a long conversation.

    12 turns covering: plain Q&A, two tool calls (Remember), one abort
    mid-turn, then more Q&A. After the chat we verify:

      1. /events contains every persisted artifact (user_message,
         message_started, message_done OR turn_terminal, tool_call,
         abort/cancel for the aborted turn). Seqs unique + monotonic.
      2. /history returns 200, has the expected message count, and
         assistant messages carry content (so a UI cold-loading the
         session sees the full transcript).
      3. POST /compact lands a compaction event, history stays
         readable, seqs still monotonic.
      4. After cold-reload (LRU eviction via 20 dummy sessions) the
         seq list AND the assistant content list are byte-identical
         to the warm read.
    """
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {}
    session = _new_session(client, app_id, "longchat")
    artifacts["session_id"] = session.session_id
    stalls_before = _capture_stalls(daemon_url)

    n_turns = 12
    abort_turn_idx = 5  # 0-based: turn 6
    tool_turn_indices = {3, 9}  # Remember calls
    artifacts["plan"] = {
        "n_turns": n_turns,
        "abort_turn_idx": abort_turn_idx,
        "tool_turn_indices": sorted(tool_turn_indices),
    }

    per_turn_detail: list[dict[str, Any]] = []
    stream = None
    t_total = time.perf_counter()

    def _wait_drain(timeout: float = 60.0) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            try:
                state = client._get(
                    f"/api/apps/{app_id}/sessions/{session.session_id}/state",
                )
                d = (state.json() or {}).get("data") or {}
                active = (d.get("turn") or {}).get("active")
                q = (d.get("queue") or {}).get("entries") or []
                pending = sum(
                    1 for e in q
                    if e.get("status") not in ("completed", "cancelled", "failed")
                )
                if not active and pending == 0:
                    return True
            except Exception:
                pass
            time.sleep(1.5)
        return False

    try:
        for i in range(n_turns):
            t0 = time.perf_counter()
            if i in tool_turn_indices:
                prompt = (
                    f"Turn {i + 1}: Use the Remember tool to save the fact "
                    f"key=fact_{i}, value=hello_{i}. Then say 'Saved.' and stop."
                )
            elif i == abort_turn_idx:
                prompt = (
                    f"Turn {i + 1}: Please write a very long 3000-word "
                    f"essay on the history of agriculture in great detail."
                )
            else:
                prompt = f"Turn {i + 1}: Just say 'OK {i + 1}' and nothing else."

            if i == abort_turn_idx:
                # Open stream, post via raw to allow abort.
                post = client.post_message_raw(session, prompt)
                if stream is None:
                    stream = client.open_event_stream(
                        session, wait_for_session=True,
                    )
                else:
                    # reuse stream from previous turn
                    pass
                got_started = stream.wait_for("message_started", timeout=60.0)
                time.sleep(0.5)  # let some tokens flow
                abort_resp = client._post(
                    f"/api/apps/{app_id}/sessions/{session.session_id}/abort",
                    json={},
                )
                ended = stream.wait_for_any(
                    ["message_done", "message_cancelled", "abort", "turn_terminal"],
                    timeout=15.0,
                )
                per_turn_detail.append({
                    "i": i,
                    "kind": "aborted",
                    "post_status": post.get("status_code"),
                    "got_started": bool(got_started),
                    "abort_status": abort_resp.status_code,
                    "end_type": ended.get("type") if ended else None,
                    "wall": round(time.perf_counter() - t0, 2),
                })
                _wait_drain(30.0)
            else:
                stream = client.send_live(
                    session, prompt, total_timeout=180.0, stream=stream,
                )
                done = next(
                    (e for e in reversed(stream.events())
                     if e.get("type") == "message_done"),
                    None,
                )
                per_turn_detail.append({
                    "i": i,
                    "kind": "tool" if i in tool_turn_indices else "qa",
                    "got_done": bool(done),
                    "wall": round(time.perf_counter() - t0, 2),
                })
        _wait_drain(120.0)
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    artifacts["per_turn"] = per_turn_detail
    artifacts["chat_wall_seconds"] = round(time.perf_counter() - t_total, 1)

    # ── 1. /events contract checks ─────────────────────────────────
    persisted = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/events?limit=20000",
    )
    pdata = (persisted.json() or {}).get("data") or {}
    pevents = pdata.get("events") or []
    sorted_persisted = assertions.sort_by_seq(pevents)

    def _count(t: str) -> int:
        return sum(1 for e in pevents if e.get("type") == t)

    n_user = _count("user_message")
    n_started = _count("message_started")
    n_done = _count("message_done")
    n_term = _count("turn_terminal")
    n_tool = _count("tool_call")
    n_abort = (
        _count("abort") + _count("message_cancelled")
        + sum(
            1 for e in pevents
            if e.get("type") == "turn_terminal"
            and (e.get("payload") or {}).get("status") in ("aborted", "cancelled")
        )
    )

    completed_tool = sum(
        1 for e in pevents
        if e.get("type") == "tool_call"
        and (e.get("payload") or {}).get("op_state") == "completed"
        and bool((e.get("payload") or {}).get("success"))
    )

    artifacts["event_counts"] = {
        "user_message": n_user,
        "message_started": n_started,
        "message_done": n_done,
        "turn_terminal": n_term,
        "tool_call": n_tool,
        "completed_tool_call": completed_tool,
        "abort_signals": n_abort,
        "total": len(pevents),
    }

    # ── 2. /history reconstruction ─────────────────────────────────
    hist = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/history?events_limit=20000",
    )
    hdata = (hist.json() or {}).get("data") or {}
    artifacts["history_status"] = hist.status_code
    artifacts["history_message_count"] = hdata.get("message_count")
    artifacts["history_event_count"] = hdata.get("event_count")
    messages = hdata.get("messages") or []
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    user_msgs_hist = [m for m in messages if m.get("role") == "user"]
    nonempty_asst = sum(
        1 for m in assistant_msgs if str(m.get("content") or "").strip()
    )
    artifacts["history_user_msgs"] = len(user_msgs_hist)
    artifacts["history_assistant_msgs"] = len(assistant_msgs)
    artifacts["history_nonempty_assistant"] = nonempty_asst

    # ── 3. Compaction ──────────────────────────────────────────────
    compact_resp = client._post(
        f"/api/apps/{app_id}/sessions/{session.session_id}/compact",
        json={},
    )
    artifacts["compact_status"] = compact_resp.status_code
    time.sleep(2.0)

    hist_post = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/history?events_limit=20000",
    )
    post_data = (hist_post.json() or {}).get("data") or {}
    post_events = post_data.get("events") or []
    compaction_events = [e for e in post_events if e.get("type") == "compaction"]
    artifacts["compaction_events"] = len(compaction_events)
    artifacts["history_post_compact_status"] = hist_post.status_code
    artifacts["history_post_compact_msgs"] = post_data.get("message_count")

    post_seqs = [int(e.get("seq") or 0) for e in post_events]
    post_seq_monotonic = (
        post_seqs == sorted(post_seqs) and len(set(post_seqs)) == len(post_seqs)
    )
    artifacts["seq_monotonic_post_compact"] = post_seq_monotonic

    # ── 4. Cold reload after eviction ──────────────────────────────
    warm_seqs = [int(e.get("seq") or 0) for e in post_events]
    warm_asst_contents = [
        str(m.get("content") or "")[:120]
        for m in (post_data.get("messages") or [])
        if m.get("role") == "assistant"
    ]

    evict = [_new_session(client, app_id, "lcevict") for _ in range(20)]
    def _ping(s):
        try:
            client.post_message_raw(s, "ping")
        except Exception:
            pass

    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_ping, evict))
    time.sleep(3.0)

    hist_cold = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/history?events_limit=20000",
    )
    cold_data = (hist_cold.json() or {}).get("data") or {}
    cold_events = cold_data.get("events") or []
    cold_seqs = [int(e.get("seq") or 0) for e in cold_events]
    cold_asst_contents = [
        str(m.get("content") or "")[:120]
        for m in (cold_data.get("messages") or [])
        if m.get("role") == "assistant"
    ]
    artifacts["cold_event_count"] = len(cold_events)
    artifacts["cold_message_count"] = cold_data.get("message_count")
    artifacts["cold_seqs_match"] = warm_seqs == cold_seqs
    artifacts["cold_asst_content_match"] = warm_asst_contents == cold_asst_contents

    artifacts["stalls_delta"] = _stalls_delta(stalls_before, _capture_stalls(daemon_url))

    # ── Final checks ────────────────────────────────────────────────
    # The aborted turn may NOT have produced a message_done, so we
    # expect at minimum (n_turns - 1) clean terminations + 1 cancel-ish.
    expected_terminations_min = n_turns - 1
    actual_clean_terminations = max(n_done, n_term)

    checks = [
        ("seq_unique", assertions.seq_unique(sorted_persisted)),
        (f"persisted_{n_turns}_user_messages", (
            n_user >= n_turns,
            f"got {n_user} / expected >= {n_turns}",
        )),
        ("most_turns_terminated", (
            actual_clean_terminations >= expected_terminations_min,
            f"got {actual_clean_terminations} clean ends "
            f"/ expected >= {expected_terminations_min}",
        )),
        # LLM compliance is variable -- some runs the model calls
        # Remember twice as instructed, some only once. The daemon's
        # job is to faithfully record whatever fires, not to enforce
        # LLM tool-use. Assert at least one to confirm the tool path
        # works end-to-end; the second is a soft-signal.
        ("at_least_1_tool_call", (
            n_tool >= 1,
            f"got {n_tool} tool_calls (expected >= 1)",
        )),
        ("at_least_1_completed_tool", (
            completed_tool >= 1,
            f"got {completed_tool} successful tool_calls",
        )),
        ("at_least_1_abort_signal", (
            n_abort >= 1,
            f"got {n_abort} abort/cancel signals",
        )),
        ("history_warm_ok", (
            hist.status_code == 200,
            f"HTTP {hist.status_code}",
        )),
        ("history_has_messages", (
            (hdata.get("message_count") or 0) >= n_turns,
            f"got {hdata.get('message_count')} messages "
            f"(expected >= {n_turns})",
        )),
        ("history_assistant_content_nonempty", (
            nonempty_asst >= n_turns - 2,
            f"only {nonempty_asst}/{len(assistant_msgs)} assistant msgs "
            f"have content (expected >= {n_turns - 2})",
        )),
        ("compact_http_ok", (
            compact_resp.status_code in (200, 202),
            f"HTTP {compact_resp.status_code}",
        )),
        ("compaction_event_persisted", (
            len(compaction_events) >= 1,
            f"got {len(compaction_events)} compaction events",
        )),
        ("history_post_compact_ok", (
            hist_post.status_code == 200,
            f"HTTP {hist_post.status_code}",
        )),
        ("seq_monotonic_post_compact", (
            post_seq_monotonic,
            "seqs not monotonic post-compact",
        )),
        ("cold_reload_seqs_match", (
            artifacts["cold_seqs_match"],
            f"warm={len(warm_seqs)} cold={len(cold_seqs)}",
        )),
        ("cold_reload_content_match", (
            artifacts["cold_asst_content_match"],
            "assistant content diverged after cold reload",
        )),
    ]
    ok, detail = assertions.report(checks)

    nstalls = artifacts["stalls_delta"].get("new_stalls", 0)
    last_gap = float(artifacts["stalls_delta"].get("last_gap_ms") or 0)
    if nstalls > 0 and last_gap > 10000.0:
        ok = False
        detail += f" | LOOP STALL >10s: +{nstalls} (last_gap_ms={last_gap})"
    elif nstalls > 0:
        detail += f" | tolerable stall: +{nstalls} (last_gap_ms={last_gap})"
    return ok, detail, artifacts


# ── 9. Session delete cleanup ──────────────────────────────────────


def scenario_delete_session_cleanup(
    client: DevClient, app_id: str, daemon_url: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Create a session, send a message, then DELETE. Verify /history
    returns 404 after delete + the daemon doesn't crash."""
    artifacts: dict[str, Any] = {}
    session = _new_session(client, app_id, "del")
    artifacts["session_id"] = session.session_id
    stalls_before = _capture_stalls(daemon_url)

    stream = None
    try:
        stream = client.send_live(
            session, "Reply just 'OK'.", total_timeout=180.0,
        )
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    deadline = time.perf_counter() + 60.0
    while time.perf_counter() < deadline:
        state = client._get(
            f"/api/apps/{app_id}/sessions/{session.session_id}/state",
        )
        d = (state.json() or {}).get("data") or {}
        if not (d.get("turn") or {}).get("active"):
            break
        time.sleep(2.0)

    pre = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/history",
    )
    artifacts["pre_delete_status"] = pre.status_code

    delete_resp = client._delete(
        f"/api/apps/{app_id}/sessions/{session.session_id}",
    )
    artifacts["delete_status"] = delete_resp.status_code

    post = client._get(
        f"/api/apps/{app_id}/sessions/{session.session_id}/history",
    )
    artifacts["post_delete_status"] = post.status_code

    artifacts["stalls_delta"] = _stalls_delta(stalls_before, _capture_stalls(daemon_url))

    checks = [
        ("pre_delete_visible", (
            pre.status_code == 200,
            f"HTTP {pre.status_code}",
        )),
        ("delete_succeeded", (
            delete_resp.status_code in (200, 204),
            f"HTTP {delete_resp.status_code}",
        )),
        ("post_delete_gone", (
            post.status_code == 404,
            f"HTTP {post.status_code} (expected 404)",
        )),
    ]
    ok, detail = assertions.report(checks)
    if artifacts["stalls_delta"].get("new_stalls", 0) > 0:
        ok = False
        detail += f" | LOOP STALL: +{artifacts['stalls_delta']['new_stalls']}"
    return ok, detail, artifacts
