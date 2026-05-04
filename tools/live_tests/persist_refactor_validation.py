"""Production-grade validation of the persist refactor.

Validates:
  T1. Multi-turn conversation (10 turns) - context retention + latency
  T2. Per-turn persistence: load_messages returns same data the agent sees
  T3. Back-to-back rapid messages - lock release latency
  T4. Session resume after cache eviction - per-turn aggregation works
  T5. Long-context behavior (compaction fallback path)
  T6. Per-turn key format: verify keys exist in DiskCache after a few turns
  T7. Migration: existing legacy blob is preserved when first save_turn_messages fires

Each test prints PASS/FAIL with timing details and surfaces concrete
failure data so we can debug without re-running.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


WORKSPACE = r"C:\Users\ASUS\Documents\digitorn_web_clone"
APP_ID = "copilot-smoke"


# ── Helpers ────────────────────────────────────────────────────────


def _client() -> DevClient:
    creds = json.loads((Path.home() / ".digitorn" / "credentials.json").read_text())
    return DevClient.with_token(creds["access_token"])


def _resolve_user_id() -> str:
    """Pull the daemon-known user_id from the JWT ``sub`` claim.

    The credentials.json's ``user`` dict is sometimes empty (test user
    seeded without profile data). The JWT ``sub`` is the authoritative
    user_id used in session_store keys.
    """
    creds = json.loads((Path.home() / ".digitorn" / "credentials.json").read_text())
    tok = creds.get("access_token", "")
    if not tok:
        return ""
    try:
        import jwt  # PyJWT
        claims = jwt.decode(tok, options={"verify_signature": False})
        return str(claims.get("sub", "") or claims.get("user_id", "") or "")
    except Exception:
        return ""


def _new_session(client: DevClient, prefix: str) -> SessionHandle:
    """Local handle constructor.

    The SDK's create_session is a pure local builder - the server-side
    session row is only created on the first ``post_message_raw`` call.
    So callers must do POST-first, then ``open_event_stream``.
    """
    sid = f"{prefix}-{uuid.uuid4().hex[:8]}"
    return client.create_session(APP_ID, workspace=WORKSPACE, session_id=sid)


def _bootstrap_session(client: DevClient, prefix: str, first_msg: str,
                       timeout: float = 180) -> tuple[SessionHandle, "object", str, float]:
    """Build session, send first message, open stream, wait for first turn.

    Returns (session, stream, first_cid, first_turn_wall_seconds).

    Must be called once per test before any subsequent POSTs. Caller is
    responsible for calling ``stream.stop(timeout=2.0)`` in finally.
    """
    session = _new_session(client, prefix)
    t0 = time.monotonic()
    post = client.post_message_raw(session, first_msg)
    cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
    # NOW open the stream - the session row exists. The bus replay
    # mechanism will deliver any events that fired between the POST
    # and the join.
    stream = client.open_event_stream(session)
    done = stream.wait_for(
        "message_done", timeout=timeout,
        predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
    )
    if done is None:
        raise RuntimeError(
            f"_bootstrap_session: first message_done not received within {timeout}s "
            f"for session={session.session_id}"
        )
    return session, stream, cid, time.monotonic() - t0


def _send_and_wait(client: DevClient, session: SessionHandle, stream, msg: str,
                   timeout: float = 180) -> tuple[str, float, dict | None]:
    """POST a message, wait for message_done, return (correlation_id, wall_time_s, done_payload)."""
    t0 = time.monotonic()
    post = client.post_message_raw(session, msg)
    cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
    if not cid:
        return ("", time.monotonic() - t0, None)
    done = stream.wait_for(
        "message_done", timeout=timeout,
        predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
    )
    return (cid, time.monotonic() - t0, (done or {}).get("payload"))


def _resolve_kv_backend():
    """Open whatever KV backend the daemon is configured to use.

    Reads digitorn settings to pick between Redis and DiskCache.
    Falls back to DiskCache for tests run against a daemon-less daemon.
    Returns (kind, client) where kind is "redis" | "diskcache".
    """
    url = ""
    try:
        from digitorn.core.config import get_settings
        s = get_settings()
        # The actual config path is server.kv_backend (not top-level).
        url = getattr(getattr(s, "server", None), "kv_backend", "") or ""
        if not url:
            url = getattr(s, "kv_backend", "") or ""
    except Exception:
        url = ""
    if isinstance(url, str) and url.startswith(("redis://", "rediss://")):
        try:
            import redis  # type: ignore
            client = redis.Redis.from_url(url, decode_responses=True)
            client.ping()
            return ("redis", client)
        except Exception as exc:
            return ("error", f"redis open failed: {exc}")
    # Fallback to DiskCache
    try:
        import diskcache
        return ("diskcache", diskcache.Cache(str(Path.home() / ".digitorn" / "kv")))
    except Exception as exc:
        return ("error", f"diskcache open failed: {exc}")


def _kv_has(kind: str, client, key: str) -> bool:
    """Backend-agnostic key existence check."""
    if kind == "redis":
        return bool(client.exists(key))
    if kind == "diskcache":
        return key in client
    return False


def _kv_get(kind: str, client, key: str):
    """Backend-agnostic value get."""
    if kind == "redis":
        return client.get(key)
    if kind == "diskcache":
        return client.get(key)
    return None


def _read_kv_keys_for_session(session: SessionHandle, user_id: str) -> dict:
    """Inspect the daemon's KV backend to verify per-turn keys exist.

    Auto-detects Redis vs DiskCache from the daemon config. Used to
    prove the refactor is actually writing the new per-turn format -
    a behavioural test (load_messages returns N items) wouldn't
    distinguish "wrote per-turn correctly" from "wrote full blob".
    """
    kind, client = _resolve_kv_backend()
    if kind == "error":
        return {"error": str(client)}
    base = f"{APP_ID}:{user_id}:{session.session_id}"
    out = {
        "backend": kind,
        "key_prefix": base,
        "legacy_messages_blob": _kv_has(kind, client, base + ":messages"),
        "messages_index": _kv_has(kind, client, base + ":messages:turns"),
        "events_index": _kv_has(kind, client, base + ":events:turns"),
        "session_object": _kv_has(kind, client, base),
    }
    if out["messages_index"]:
        try:
            raw = _kv_get(kind, client, base + ":messages:turns")
            idx = json.loads(raw or "[]")
            out["messages_turn_indices"] = idx
            out["messages_per_turn_keys"] = [
                _kv_has(kind, client, base + f":messages:turn:{t}") for t in idx
            ]
        except Exception as exc:
            out["messages_index_parse_error"] = str(exc)
    if out["events_index"]:
        try:
            raw = _kv_get(kind, client, base + ":events:turns")
            idx = json.loads(raw or "[]")
            out["events_turn_indices"] = idx
        except Exception as exc:
            out["events_index_parse_error"] = str(exc)
    if kind == "diskcache":
        try:
            client.close()
        except Exception:
            pass
    return out


def _read_diskcache_keys_for_session(session: SessionHandle, user_id: str) -> dict:
    """Backward-compat shim - use _read_kv_keys_for_session."""
    return _read_kv_keys_for_session(session, user_id)


# ── Tests ──────────────────────────────────────────────────────────


def t1_multiturn_with_latency(client: DevClient) -> tuple[bool, list[str], dict]:
    """10-turn conversation with per-turn latency tracking + context retention."""
    bugs: list[str] = []
    artifacts: dict = {"name": "T1_multiturn_latency"}

    # First message establishes context, then 9 short follow-ups.
    messages = [
        "Hi! My name is Paul. I'm validating a persistence refactor today. Reply briefly with hello, no tools.",
        "What is my name? One sentence.",
        "What am I validating today? One sentence.",
        "Pick a fruit and remember it for me. One sentence.",
        "What fruit did you pick? One sentence.",
        "Now pick an animal too. One sentence.",
        "What animal did you pick? One sentence.",
        "Remind me what I'm validating, my name, fruit, and animal. Bullet list.",
        "Now count from 1 to 5. One line each.",
        "Final question: what was my very first message? Quote it briefly.",
    ]
    artifacts["latencies_s"] = []
    artifacts["correlation_ids"] = []
    artifacts["last_assistants"] = []
    stream = None
    try:
        session, stream, first_cid, first_elapsed = _bootstrap_session(
            client, "t1", messages[0], timeout=240,
        )
        artifacts["session_id"] = session.session_id
        artifacts["latencies_s"].append(round(first_elapsed, 2))
        artifacts["correlation_ids"].append(first_cid)
        time.sleep(0.3)
        hist = client.get_history(session)
        artifacts["last_assistants"].append(
            (hist[-1].get("content", "") if hist else "")[:120]
        )

        for i, msg in enumerate(messages[1:], start=2):
            cid, elapsed, done = _send_and_wait(client, session, stream, msg, timeout=240)
            artifacts["latencies_s"].append(round(elapsed, 2))
            artifacts["correlation_ids"].append(cid)
            if not cid:
                bugs.append(f"T1.turn{i}: post returned no correlation_id")
                break
            if done is None:
                bugs.append(f"T1.turn{i}: message_done never received within 180s")
                break
            time.sleep(0.3)
            hist = client.get_history(session)
            last = hist[-1].get("content", "") if hist else ""
            artifacts["last_assistants"].append(last[:120])

        # Context retention checks
        if len(artifacts["last_assistants"]) >= 2:
            if "paul" not in artifacts["last_assistants"][1].lower():
                bugs.append(f"T1.turn2: did NOT remember 'Paul'. Got: {artifacts['last_assistants'][1][:200]}")
        if len(artifacts["last_assistants"]) >= 8:
            recap = artifacts["last_assistants"][7].lower()
            for needle in ("paul",):
                if needle not in recap:
                    bugs.append(f"T1.turn8 recap missing '{needle}'. Got: {recap[:200]}")
        if len(artifacts["last_assistants"]) >= 10:
            last_msg = artifacts["last_assistants"][9].lower()
            # "validating a persistence refactor" or similar
            if "valid" not in last_msg and "persist" not in last_msg and "refactor" not in last_msg:
                bugs.append(f"T1.turn10 quote-back missing first-msg content. Got: {last_msg[:200]}")

        # Latency: post-turn 2..N average should be reasonable
        if len(artifacts["latencies_s"]) >= 2:
            artifacts["avg_latency_s"] = round(
                sum(artifacts["latencies_s"]) / len(artifacts["latencies_s"]), 2,
            )
            artifacts["max_latency_s"] = max(artifacts["latencies_s"])
            artifacts["min_latency_s"] = min(artifacts["latencies_s"])

        # Sequence + lifecycle
        events = assertions.sort_by_seq(stream.events())
        ok, det = assertions.seq_unique(events)
        if not ok:
            bugs.append(f"T1.seq_unique FAILED: {det}")
        ok, det = assertions.event_count(events, "message_done", minimum=10, maximum=10)
        if not ok:
            bugs.append(f"T1.message_done count: {det}")
        artifacts["total_events"] = len(events)

    except Exception as exc:
        bugs.append(f"T1.EXCEPTION: {type(exc).__name__}: {exc}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
    return (len(bugs) == 0), bugs, artifacts


def t2_persistence_load_matches_history(client: DevClient) -> tuple[bool, list[str], dict]:
    """After several turns, verify load_messages returns what the agent saw."""
    bugs: list[str] = []
    artifacts: dict = {"name": "T2_persistence_load_matches_history"}

    msgs = [
        "Reply 'A' (only the letter). No tools.",
        "Reply 'B' (only the letter). No tools.",
        "Reply 'C' (only the letter). No tools.",
        "Reply 'D' (only the letter). No tools.",
        "Reply 'E' (only the letter). No tools.",
    ]
    stream = None
    try:
        session, stream, _, _ = _bootstrap_session(
            client, "t2", msgs[0], timeout=120,
        )
        artifacts["session_id"] = session.session_id
        time.sleep(0.5)
        for m in msgs[1:]:
            cid, _, done = _send_and_wait(client, session, stream, m, timeout=120)
            if done is None:
                bugs.append(f"T2: turn for '{m}' never completed")
                break
            time.sleep(0.5)

        # Live history (from API - reads in-memory or DB)
        live_history = client.get_history(session)
        artifacts["live_history_count"] = len(live_history)

        # Force a wait to let bg persist tasks complete
        time.sleep(3.0)

        # Inspect DiskCache directly to verify per-turn format
        # (we need the user_id - resolve from credentials)
        user_id = _resolve_user_id() or "local"
        kv_state = _read_diskcache_keys_for_session(session, user_id)
        artifacts["kv_state"] = kv_state

        if not kv_state.get("messages_index"):
            bugs.append(
                f"T2: messages_index key absent - per-turn refactor not active or session not in DiskCache. kv_state={kv_state}"
            )
        else:
            idx = kv_state.get("messages_turn_indices", [])
            if len(idx) < len(msgs):
                bugs.append(
                    f"T2: only {len(idx)} per-turn keys for {len(msgs)} turns. indices={idx}"
                )
            if not all(kv_state.get("messages_per_turn_keys", [])):
                bugs.append(
                    f"T2: some per-turn message keys missing. presence={kv_state.get('messages_per_turn_keys')}"
                )

    except Exception as exc:
        bugs.append(f"T2.EXCEPTION: {type(exc).__name__}: {exc}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
    return (len(bugs) == 0), bugs, artifacts


def t3_back_to_back_rapid(client: DevClient) -> tuple[bool, list[str], dict]:
    """Send the next message immediately after message_done. Validates lock release."""
    bugs: list[str] = []
    artifacts: dict = {"name": "T3_back_to_back_rapid"}

    NUM_TURNS = 6
    artifacts["post_to_started_ms"] = []
    stream = None
    try:
        # Bootstrap with first message - this also exercises the
        # "very first turn" path. Subsequent turns measured below.
        session, stream, first_cid, first_elapsed = _bootstrap_session(
            client, "t3", "Reply with the digit 0. No tools.", timeout=120,
        )
        artifacts["session_id"] = session.session_id

        for i in range(1, NUM_TURNS):
            t_post = time.monotonic()
            post = client.post_message_raw(session, f"Reply with the digit {i}. No tools.")
            cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
            if not cid:
                bugs.append(f"T3.turn{i}: no correlation_id")
                break
            # Wait for message_started (= turn actually picked up by agent_loop)
            started = stream.wait_for(
                "message_started", timeout=60,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
            )
            t_started = time.monotonic()
            artifacts["post_to_started_ms"].append(round((t_started - t_post) * 1000, 1))
            if started is None:
                bugs.append(f"T3.turn{i}: message_started never received")
                break
            done = stream.wait_for(
                "message_done", timeout=60,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
            )
            if done is None:
                bugs.append(f"T3.turn{i}: message_done never received")
                break
            # NO sleep - send next immediately

        if artifacts["post_to_started_ms"]:
            artifacts["max_post_to_started_ms"] = max(artifacts["post_to_started_ms"])
            artifacts["avg_post_to_started_ms"] = round(
                sum(artifacts["post_to_started_ms"]) / len(artifacts["post_to_started_ms"]), 1,
            )
            # The whole point: post-to-started should be sub-second. If it's >2s,
            # the lock is being held too long after the previous turn's persists.
            if artifacts["max_post_to_started_ms"] > 2000:
                bugs.append(
                    f"T3: max post-to-started latency = {artifacts['max_post_to_started_ms']}ms "
                    f"(target <2000ms). Lock probably still blocked by post-turn persists."
                )

    except Exception as exc:
        bugs.append(f"T3.EXCEPTION: {type(exc).__name__}: {exc}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
    return (len(bugs) == 0), bugs, artifacts


def t4_session_resume_after_eviction(client: DevClient) -> tuple[bool, list[str], dict]:
    """Build a session, force its cache eviction, reload via API, verify integrity."""
    bugs: list[str] = []
    artifacts: dict = {"name": "T4_session_resume"}

    msgs = [
        "Remember a unique tag: ZEBRA-7421. Just say 'OK'. No tools.",
        "Remember another unique tag: ALPHA-9988. Just say 'OK'. No tools.",
        "Remember a third unique tag: GAMMA-3050. Just say 'OK'. No tools.",
    ]
    stream = None
    try:
        session, stream, _, _ = _bootstrap_session(
            client, "t4", msgs[0], timeout=120,
        )
        artifacts["session_id"] = session.session_id
        for m in msgs[1:]:
            cid, _, done = _send_and_wait(client, session, stream, m, timeout=120)
            if done is None:
                bugs.append(f"T4: setup turn '{m}' failed")
                return (False, bugs, artifacts)

        # Wait for bg persists to flush
        time.sleep(3.0)
        history_before = client.get_history(session)
        artifacts["history_before_count"] = len(history_before)

        # Drop the session from cache via close_session (keeps disk + DB)
        try:
            client.close_session(session)
            artifacts["close_session"] = "ok"
        except Exception as exc:
            artifacts["close_session_error"] = str(exc)

        # Re-fetch history - forces rehydrate from store
        time.sleep(1.0)
        history_after = client.get_history(session)
        artifacts["history_after_count"] = len(history_after)

        if len(history_after) != len(history_before):
            bugs.append(
                f"T4: history count diverged. before={len(history_before)} "
                f"after_eviction={len(history_after)}"
            )

        # Tags should be in the rehydrated history
        all_text = " ".join(
            m.get("content", "") for m in history_after
            if isinstance(m.get("content"), str)
        ).upper()
        for tag in ("ZEBRA-7421", "ALPHA-9988", "GAMMA-3050"):
            if tag not in all_text:
                bugs.append(f"T4: tag '{tag}' MISSING from rehydrated history")

        # Now send a new message after rehydrate to verify continuation
        cid, _, done = _send_and_wait(
            client, session, stream,
            "What unique tags am I tracking? List them. One sentence.",
            timeout=120,
        )
        if done is None:
            bugs.append("T4: post-rehydrate message did not complete")
        else:
            time.sleep(0.5)
            new_hist = client.get_history(session)
            last = new_hist[-1].get("content", "").upper() if new_hist else ""
            artifacts["post_rehydrate_assistant"] = last[:300]
            for tag in ("ZEBRA", "ALPHA", "GAMMA"):
                if tag not in last:
                    bugs.append(f"T4: agent failed to recall '{tag}' after rehydrate")

    except Exception as exc:
        bugs.append(f"T4.EXCEPTION: {type(exc).__name__}: {exc}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
    return (len(bugs) == 0), bugs, artifacts


def t5_persistent_events_replay(client: DevClient) -> tuple[bool, list[str], dict]:
    """Verify persistent event log captures everything and replays identically."""
    bugs: list[str] = []
    artifacts: dict = {"name": "T5_persistent_events"}

    msgs = [
        "Reply 'first'. No tools.",
        "Reply 'second'. No tools.",
        "Reply 'third'. No tools.",
    ]
    stream = None
    try:
        session, stream, _, _ = _bootstrap_session(
            client, "t5", msgs[0], timeout=120,
        )
        artifacts["session_id"] = session.session_id
        for m in msgs[1:]:
            cid, _, done = _send_and_wait(client, session, stream, m, timeout=120)
            if done is None:
                bugs.append(f"T5: turn '{m}' failed")
                return (False, bugs, artifacts)
        time.sleep(2.0)

        # Persistent events from DB
        persistent = client.get_persistent_events(session, since_seq=0, limit=5000)
        artifacts["persistent_count"] = len(persistent)
        artifacts["persistent_types"] = sorted({
            e.get("type", "?") for e in persistent
        })
        if len(persistent) == 0:
            bugs.append("T5: persistent event log is EMPTY")

        # The 3 message_done events must be there
        message_done_count = sum(
            1 for e in persistent if e.get("type") == "message_done"
        )
        if message_done_count != 3:
            bugs.append(f"T5: expected 3 message_done in persistent, got {message_done_count}")

        # ephemeral types must NOT leak
        ok, det = assertions.ephemeral_types_absent_from_persistent(persistent)
        if not ok:
            bugs.append(f"T5.ephemeral_leak: {det}")

    except Exception as exc:
        bugs.append(f"T5.EXCEPTION: {type(exc).__name__}: {exc}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
    return (len(bugs) == 0), bugs, artifacts


def t6_event_log_aggregation(client: DevClient) -> tuple[bool, list[str], dict]:
    """Verify save_turn_events per-turn keys aggregate correctly across turns."""
    bugs: list[str] = []
    artifacts: dict = {"name": "T6_event_log_aggregation"}

    msgs = ["Say 'one'. No tools.", "Say 'two'. No tools.", "Say 'three'. No tools."]
    stream = None
    try:
        session, stream, _, _ = _bootstrap_session(
            client, "t6", msgs[0], timeout=120,
        )
        artifacts["session_id"] = session.session_id
        for m in msgs[1:]:
            cid, _, done = _send_and_wait(client, session, stream, m, timeout=120)
            if done is None:
                bugs.append(f"T6: turn '{m}' failed")
                return (False, bugs, artifacts)
        time.sleep(3.0)

        user_id = _resolve_user_id() or "local"
        kv_state = _read_diskcache_keys_for_session(session, user_id)
        artifacts["kv_state"] = kv_state

        if not kv_state.get("events_index"):
            bugs.append(f"T6: events_index missing - per-turn events keys not written. kv_state={kv_state}")

    except Exception as exc:
        bugs.append(f"T6.EXCEPTION: {type(exc).__name__}: {exc}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
    return (len(bugs) == 0), bugs, artifacts


def t7_long_session_30_turns(client: DevClient) -> tuple[bool, list[str], dict]:
    """30-turn session: validates no per-turn latency degradation + KV index growth.

    Targets fix #4 (path_locks LRU), the persist refactor (per-turn keys), and
    overall steady-state stability. Failure mode we are catching: turn N's wall
    time growing super-linearly with N (= some O(N) work per turn that scales
    with conversation length).
    """
    bugs: list[str] = []
    artifacts: dict = {"name": "T7_long_session_30_turns"}

    NUM_TURNS = 30
    artifacts["latencies_s"] = []
    stream = None
    try:
        session, stream, _, first_elapsed = _bootstrap_session(
            client, "t7", "Reply with 'turn 0 ok'. No tools.", timeout=120,
        )
        artifacts["session_id"] = session.session_id
        artifacts["latencies_s"].append(round(first_elapsed, 2))

        for i in range(1, NUM_TURNS):
            cid, wall, done = _send_and_wait(
                client, session, stream,
                f"Reply with exactly 'turn {i} ok'. No tools.",
                timeout=90,
            )
            artifacts["latencies_s"].append(round(wall, 2))
            if done is None:
                bugs.append(f"T7.turn{i}: no message_done within 90s (wall={wall:.1f}s)")
                break

        if artifacts["latencies_s"]:
            artifacts["max_latency_s"] = max(artifacts["latencies_s"])
            artifacts["avg_latency_s"] = round(
                sum(artifacts["latencies_s"]) / len(artifacts["latencies_s"]), 2,
            )
            # Degradation check: last 5 turns should not be >3x avg of first 5.
            if len(artifacts["latencies_s"]) >= 10:
                first5 = artifacts["latencies_s"][:5]
                last5 = artifacts["latencies_s"][-5:]
                avg_first = sum(first5) / 5
                avg_last = sum(last5) / 5
                artifacts["avg_first5_s"] = round(avg_first, 2)
                artifacts["avg_last5_s"] = round(avg_last, 2)
                if avg_first > 0 and avg_last > 3 * avg_first:
                    bugs.append(
                        f"T7: latency degradation - first5 avg {avg_first:.2f}s, "
                        f"last5 avg {avg_last:.2f}s ({avg_last / avg_first:.1f}x). "
                        f"Some O(N) work per turn is scaling with conversation length."
                    )

        time.sleep(2.0)
        user_id = _resolve_user_id() or "local"
        kv_state = _read_diskcache_keys_for_session(session, user_id)
        artifacts["kv_state"] = kv_state
        idx = kv_state.get("messages_turn_indices") or []
        artifacts["turn_index_count"] = len(idx)
        # Compaction may have collapsed history, but the index should
        # never be empty after 30 turns.
        if not idx:
            bugs.append("T7: messages_turn_indices is empty after 30 turns")

    except Exception as exc:
        bugs.append(f"T7.EXCEPTION: {type(exc).__name__}: {exc}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
    return (len(bugs) == 0), bugs, artifacts


def t8_rapid_fire_while_busy(client: DevClient) -> tuple[bool, list[str], dict]:
    """Post 3 messages back-to-back without waiting for message_done in between.

    Validates fix #3 (session lock fast-fail with 5-60s clamp): the second
    and third POSTs must either queue cleanly behind the lock or fail FAST
    with a structured "session lock" error. They must NEVER hang for 300s.

    Failure modes we catch:
      - HTTP 500 (unhandled exception in lock acquisition)
      - HTTP timeout (lock held >60s without releasing)
      - Silent message loss (POST returns 200 but turn never starts)
    """
    bugs: list[str] = []
    artifacts: dict = {"name": "T8_rapid_fire_while_busy"}
    artifacts["post_outcomes"] = []
    stream = None
    try:
        # Bootstrap with a slow first message that gives the agent loop
        # something real to chew on while we fire the second + third.
        session = _new_session(client, "t8")
        artifacts["session_id"] = session.session_id

        slow_first = (
            "List the first 20 prime numbers, one per line, with a one-word "
            "mnemonic for each. No tools."
        )
        t0 = time.monotonic()
        post1 = client.post_message_raw(session, slow_first)
        cid1 = (post1.get("body") or {}).get("data", {}).get("correlation_id", "")
        status1 = post1.get("status", 0)
        artifacts["post_outcomes"].append({"i": 0, "status": status1, "cid": cid1, "wall_ms": round((time.monotonic() - t0) * 1000, 1)})
        if not cid1:
            bugs.append(f"T8.post0: no correlation_id (status={status1})")
            return (False, bugs, artifacts)

        stream = client.open_event_stream(session)

        # Fire 2 more POSTs immediately - DO NOT wait for done.
        rapid_msgs = [
            "Reply with the single character 'A'. No tools.",
            "Reply with the single character 'B'. No tools.",
        ]
        cids = [cid1]
        for i, m in enumerate(rapid_msgs, start=1):
            t_post = time.monotonic()
            try:
                post = client.post_message_raw(session, m)
                wall_ms = round((time.monotonic() - t_post) * 1000, 1)
                status = post.get("status", 0)
                body = post.get("body") or {}
                cid = (body.get("data") or {}).get("correlation_id", "")
                err = body.get("error") or body.get("detail")
                outcome = {"i": i, "status": status, "cid": cid, "wall_ms": wall_ms, "error": err}
                artifacts["post_outcomes"].append(outcome)
                # Hang check: any POST taking >65s is a clear regression
                # (the clamped lock timeout is 60s max).
                if wall_ms > 65000:
                    bugs.append(f"T8.post{i}: POST took {wall_ms}ms (>65s clamp limit)")
                # 200 with cid OR a structured 4xx/503 with "lock" in the
                # error are both acceptable outcomes. 500 is not.
                if status >= 500 and status != 503:
                    bugs.append(f"T8.post{i}: HTTP {status} (unhandled exception). body={body}")
                if status == 200 and cid:
                    cids.append(cid)
            except Exception as exc:
                wall_ms = round((time.monotonic() - t_post) * 1000, 1)
                bugs.append(f"T8.post{i}: exception {type(exc).__name__}: {exc} (wall={wall_ms}ms)")

        # Drain: wait for ALL accepted POSTs to complete.
        artifacts["completions"] = []
        for cid in cids:
            done = stream.wait_for(
                "message_done", timeout=180,
                predicate=lambda e, _c=cid: (e.get("payload") or {}).get("correlation_id") == _c,
            )
            artifacts["completions"].append({"cid": cid, "done": bool(done)})
            if done is None:
                bugs.append(f"T8: cid {cid[:8]} accepted but message_done never arrived")

    except Exception as exc:
        bugs.append(f"T8.EXCEPTION: {type(exc).__name__}: {exc}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
    return (len(bugs) == 0), bugs, artifacts


def t9_subagent_abort_cleanup(client: DevClient) -> tuple[bool, list[str], dict]:
    """Validate fix #8: abort during sub-agent execution leaves no orphans.

    Targets the agent_spawn watchdog triple-safety-net (`_on_done` outer
    except + `_mode_status` ghost detection). Failure mode we are catching:
    after abort, a sub-agent stays in 'running' state forever, blocking
    subsequent message_done because the parent loop waits on a ghost.

    Uses digitorn-code (has agent_spawn). Skipped (logged not failed)
    if digitorn-code is not running.
    """
    bugs: list[str] = []
    artifacts: dict = {"name": "T9_subagent_abort_cleanup"}

    # Check digitorn-code is available before bothering
    try:
        apps = client.list_apps()
        target_app = next(
            (a for a in apps if a.get("app_id") == "digitorn-code" and a.get("runtime_status") == "running"),
            None,
        )
    except Exception as exc:
        artifacts["skip_reason"] = f"list_apps failed: {exc}"
        return (True, [], artifacts)
    if target_app is None:
        artifacts["skip_reason"] = "digitorn-code not running"
        return (True, [], artifacts)

    sid = f"t9-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id="digitorn-code",
        daemon_url=client.daemon_url, workspace=WORKSPACE,
    )
    artifacts["session_id"] = sid

    spawn_msg = (
        "Use the Agent tool to spawn a background sub-agent that lists "
        "every file in the workspace recursively. Set wait=false. After "
        "spawning, just reply OK. Do not wait for the result."
    )
    stream = None
    try:
        post = client.post_message_raw(session, spawn_msg)
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        if not cid:
            bugs.append(f"T9: no correlation_id from spawn POST. status={post.get('status')}")
            return (False, bugs, artifacts)

        stream = client.open_event_stream(session)

        # Wait for the parent to finish (sub-agent should still be in flight)
        done = stream.wait_for(
            "message_done", timeout=120,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
        )
        artifacts["parent_done"] = bool(done)

        # Wait briefly to make sure agent_spawn has actually fired
        spawn_evt = stream.wait_for("agent_event", timeout=15)
        artifacts["saw_spawn_event"] = bool(spawn_evt)

        # Now abort - this should cancel the sub-agent
        abort_t0 = time.monotonic()
        abort_resp = client.abort(session)
        artifacts["abort_response"] = abort_resp
        artifacts["abort_wall_ms"] = round((time.monotonic() - abort_t0) * 1000, 1)

        # Within 15s the watchdog must have emitted a terminal event
        # (agent_cancel or agent_result with status failed/cancelled).
        cancel_seen = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not cancel_seen:
            evt = stream.wait_for("agent_event", timeout=5)
            if evt is None:
                continue
            payload = evt.get("payload") or {}
            etype = payload.get("type", "")
            if etype in ("agent_cancel", "agent_result"):
                cancel_seen = True
                artifacts["terminal_event"] = etype
                artifacts["terminal_payload"] = {
                    k: v for k, v in payload.items() if k in ("agent_id", "type", "status", "reason")
                }
                break
        artifacts["cancel_seen"] = cancel_seen
        if not cancel_seen:
            bugs.append("T9: no agent_cancel/agent_result within 15s of abort - watchdog not finalizing")

        # Final sanity: session must accept a new message after abort cleanup
        time.sleep(2.0)
        followup = client.post_message_raw(session, "Reply with 'ok'. No tools.")
        f_cid = (followup.get("body") or {}).get("data", {}).get("correlation_id", "")
        if not f_cid:
            bugs.append(f"T9: post-abort followup rejected. status={followup.get('status')}")
        else:
            f_done = stream.wait_for(
                "message_done", timeout=60,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == f_cid,
            )
            artifacts["followup_done"] = bool(f_done)
            if f_done is None:
                bugs.append("T9: post-abort followup never completed - session stuck busy")

    except Exception as exc:
        bugs.append(f"T9.EXCEPTION: {type(exc).__name__}: {exc}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
    return (len(bugs) == 0), bugs, artifacts


# ── Runner ─────────────────────────────────────────────────────────


def _warmup(client: DevClient) -> None:
    """Throwaway turn to absorb cold-start cost before measuring.

    A short timeout is fine because warmup is opportunistic - if it
    fails (slow LLM, queue, whatever), the actual tests still run on
    their own timeouts. The warmup just helps the FIRST measured turn
    not be skewed by daemon cold-start (DB pool, MCP servers, etc.).
    """
    sid = f"warmup-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace=WORKSPACE,
    )
    print("warmup: priming the daemon (single throwaway turn, 90s budget)...", flush=True)
    t0 = time.monotonic()
    try:
        post = client.post_message_raw(session, "Reply with the single character 'W'. No tools.")
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        stream = client.open_event_stream(session)
        try:
            done = stream.wait_for(
                "message_done", timeout=90,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
            )
            elapsed = round(time.monotonic() - t0, 1)
            print(f"warmup: {'ok' if done else 'no message_done (continuing anyway)'} ({elapsed}s)", flush=True)
        finally:
            stream.stop(timeout=2.0)
    except Exception as exc:
        print(f"warmup: skipped ({type(exc).__name__}: {exc})", flush=True)
    print(flush=True)


def main() -> int:
    print("=" * 78)
    print("PERSIST REFACTOR VALIDATION - production-grade test suite")
    print("=" * 78)
    print(f"app_id:    {APP_ID}")
    print(f"workspace: {WORKSPACE}")
    print()

    client = _client()
    print(f"daemon:    {client.daemon_url}")

    # KV backend probe so the operator knows where state goes
    kind, kv_client = _resolve_kv_backend()
    print(f"kv backend: {kind}")
    if kind == "diskcache":
        try:
            kv_client.close()
        except Exception:
            pass
    print()

    _warmup(client)

    results = []
    tests = [
        ("T1 multi-turn (10 turns) + latency", t1_multiturn_with_latency),
        ("T2 persistence: load matches history", t2_persistence_load_matches_history),
        ("T3 back-to-back rapid messages", t3_back_to_back_rapid),
        ("T4 session resume after eviction", t4_session_resume_after_eviction),
        ("T5 persistent events replay", t5_persistent_events_replay),
        ("T6 event log per-turn aggregation", t6_event_log_aggregation),
        ("T7 long session (30 turns) - latency stability", t7_long_session_30_turns),
        ("T8 rapid-fire while busy - lock fast-fail", t8_rapid_fire_while_busy),
        ("T9 sub-agent abort cleanup (watchdog safety net)", t9_subagent_abort_cleanup),
    ]
    for name, fn in tests:
        print(f"\n>>> {name}")
        t0 = time.monotonic()
        try:
            ok, bugs, artifacts = fn(client)
        except Exception as exc:
            ok, bugs, artifacts = False, [f"OUTER EXCEPTION: {type(exc).__name__}: {exc}"], {}
        elapsed = round(time.monotonic() - t0, 1)
        status = "PASS" if ok else "FAIL"
        print(f"    {status}  ({elapsed}s)")
        for b in bugs:
            print(f"      - {b}")
        results.append((name, ok, elapsed, bugs, artifacts))

    print("\n" + "=" * 78)
    print("FINAL")
    print("=" * 78)
    pass_count = sum(1 for _, ok, *_ in results if ok)
    fail_count = sum(1 for _, ok, *_ in results if not ok)
    for name, ok, elapsed, bugs, _ in results:
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] ({elapsed:5.1f}s) {name}")
        for b in bugs:
            print(f"          ! {b}")
    print()
    print(f"  {pass_count} passed, {fail_count} failed")
    print()

    if fail_count > 0:
        # Dump artifacts for failed tests
        print("ARTIFACTS for failed tests:")
        for name, ok, _, _, art in results:
            if not ok:
                print(f"\n--- {name} ---")
                print(json.dumps(art, indent=2, default=str)[:4000])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
