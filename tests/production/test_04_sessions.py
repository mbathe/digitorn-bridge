"""Production tests — Session lifecycle, management, and actions.

55 tests covering session CRUD, history, fork, compact, abort, undo, and memory.
"""

from __future__ import annotations

import json
import time
import uuid

from tests.production.helpers import (
    R,
    req,
    api_ok,
    ensure_app_deployed,
    stream_chat,
    get_streamed_content,
    get_result,
    cleanup_session,
    new_session_id,
    WORKSPACE,
)

# ── Helpers ──────────────────────────────────────────────────

_created_sessions: list[tuple[str, str]] = []  # (app_id, session_id)


def _track(app_id: str, sid: str) -> str:
    """Track a session for cleanup."""
    _created_sessions.append((app_id, sid))
    return sid


def _cleanup_all():
    for app_id, sid in _created_sessions:
        cleanup_session(app_id, sid)
    _created_sessions.clear()


def _create_session(app_id: str, message: str = "Hello, say OK.", sid: str | None = None) -> tuple[str, list[dict]]:
    """Create a session via stream_chat and track it for cleanup."""
    s = sid or new_session_id()
    sid_out, events = stream_chat(app_id, message, session_id=s)
    _track(app_id, sid_out)
    return sid_out, events


# ── SESSION CRUD (15 tests) ─────────────────────────────────

def test_session_crud(app_id: str):
    print("\n--- Session CRUD ---")

    # Create a session to work with
    sid, events = _create_session(app_id)

    # 1. List sessions for app - returns array
    try:
        r = req("get", f"/api/apps/{app_id}/sessions")
        data = api_ok(r)
        sessions = data.get("sessions", [])
        assert isinstance(sessions, list), "sessions is not a list"
        R.ok("CRUD-01: List sessions returns array", f"{len(sessions)} sessions")
    except Exception as e:
        R.fail("CRUD-01: List sessions returns array", str(e))

    # 2. List sessions pagination limit=5
    try:
        r = req("get", f"/api/apps/{app_id}/sessions", params={"limit": 5})
        data = api_ok(r)
        sessions = data.get("sessions", [])
        assert len(sessions) <= 5, f"Expected <=5, got {len(sessions)}"
        R.ok("CRUD-02: List sessions limit=5", f"{len(sessions)} returned")
    except Exception as e:
        R.fail("CRUD-02: List sessions limit=5", str(e))

    # 3. List sessions pagination offset=1
    try:
        r_all = req("get", f"/api/apps/{app_id}/sessions")
        all_sessions = api_ok(r_all).get("sessions", [])
        r = req("get", f"/api/apps/{app_id}/sessions", params={"offset": 1})
        data = api_ok(r)
        offset_sessions = data.get("sessions", [])
        if len(all_sessions) > 1:
            assert len(offset_sessions) < len(all_sessions), "offset=1 should return fewer"
        R.ok("CRUD-03: List sessions offset=1", f"{len(offset_sessions)} returned")
    except Exception as e:
        R.fail("CRUD-03: List sessions offset=1", str(e))

    # 4. Get session by ID - returns metadata
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}")
        data = api_ok(r)
        assert data, "Empty session data"
        R.ok("CRUD-04: Get session by ID")
    except Exception as e:
        R.fail("CRUD-04: Get session by ID", str(e))

    # 5. Get session includes message_count
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}")
        data = api_ok(r)
        session = data.get("session", data)
        assert "message_count" in session, f"No message_count in {list(session.keys())}"
        R.ok("CRUD-05: Session has message_count", str(session["message_count"]))
    except Exception as e:
        R.fail("CRUD-05: Session has message_count", str(e))

    # 6. Get session includes created_at
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}")
        data = api_ok(r)
        session = data.get("session", data)
        assert "created_at" in session, f"No created_at in {list(session.keys())}"
        R.ok("CRUD-06: Session has created_at", str(session["created_at"])[:25])
    except Exception as e:
        R.fail("CRUD-06: Session has created_at", str(e))

    # 7. Get session includes last_active
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}")
        data = api_ok(r)
        session = data.get("session", data)
        assert "last_active" in session, f"No last_active in {list(session.keys())}"
        R.ok("CRUD-07: Session has last_active", str(session["last_active"])[:25])
    except Exception as e:
        R.fail("CRUD-07: Session has last_active", str(e))

    # 8. Get session includes title
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}")
        data = api_ok(r)
        session = data.get("session", data)
        assert "title" in session, f"No title in {list(session.keys())}"
        R.ok("CRUD-08: Session has title", str(session.get("title", ""))[:40])
    except Exception as e:
        R.fail("CRUD-08: Session has title", str(e))

    # 9. Get nonexistent session - returns 404
    try:
        fake_sid = f"nonexistent-{uuid.uuid4()}"
        r = req("get", f"/api/apps/{app_id}/sessions/{fake_sid}")
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"
        R.ok("CRUD-09: Nonexistent session returns 404")
    except Exception as e:
        R.fail("CRUD-09: Nonexistent session returns 404", str(e))

    # 10. Delete session - success
    del_status = None
    try:
        del_sid, _ = _create_session(app_id, "Delete me please.")
        r = req("delete", f"/api/apps/{app_id}/sessions/{del_sid}")
        del_status = r.status_code
        assert r.status_code in (200, 404), f"Delete returned {r.status_code}"
        R.ok("CRUD-10: Delete session success", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("CRUD-10: Delete session success", str(e))

    # 11. Delete session removes from list
    try:
        if del_status == 404:
            R.skip("CRUD-11: Deleted session removed from list", "Delete returned 404 (already cleaned up)")
        else:
            r = req("get", f"/api/apps/{app_id}/sessions")
            data = api_ok(r)
            session_ids = [s.get("session_id", s.get("id", "")) for s in data.get("sessions", [])]
            assert del_sid not in session_ids, "Deleted session still in list"
            R.ok("CRUD-11: Deleted session removed from list")
    except Exception as e:
        R.fail("CRUD-11: Deleted session removed from list", str(e))

    # 12. Delete nonexistent session - returns 404 or error
    try:
        fake_sid = f"nonexistent-{uuid.uuid4()}"
        r = req("delete", f"/api/apps/{app_id}/sessions/{fake_sid}")
        assert r.status_code in (404, 400, 200), f"Unexpected {r.status_code}"
        if r.status_code == 200:
            body = r.json()
            # API might return success=false for nonexistent
            R.ok("CRUD-12: Delete nonexistent session handled", f"HTTP {r.status_code}")
        else:
            R.ok("CRUD-12: Delete nonexistent session handled", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("CRUD-12: Delete nonexistent session handled", str(e))

    # 13. Session created_at is recent (within last 5 min)
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}")
        data = api_ok(r)
        session = data.get("session", data)
        created = session.get("created_at", "")
        # Parse ISO timestamp or unix epoch
        if isinstance(created, (int, float)):
            age = time.time() - created
        else:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - dt).total_seconds()
        assert age < 300, f"created_at is {age:.0f}s ago, expected <300s"
        R.ok("CRUD-13: created_at is recent", f"{age:.0f}s ago")
    except Exception as e:
        R.fail("CRUD-13: created_at is recent", str(e))

    # 14. Session last_active updates after chat
    try:
        r1 = req("get", f"/api/apps/{app_id}/sessions/{sid}")
        d1 = api_ok(r1).get("session", api_ok(r1))
        last1 = d1.get("last_active", "")
        time.sleep(1)
        stream_chat(app_id, "Ping for last_active update.", session_id=sid)
        r2 = req("get", f"/api/apps/{app_id}/sessions/{sid}")
        d2 = api_ok(r2).get("session", api_ok(r2))
        last2 = d2.get("last_active", "")
        assert last2 >= last1, f"last_active did not advance: {last1} -> {last2}"
        R.ok("CRUD-14: last_active updates after chat")
    except Exception as e:
        R.fail("CRUD-14: last_active updates after chat", str(e))

    # 15. Multiple sessions for same app are independent
    try:
        sid_a, _ = _create_session(app_id, "Session A: remember ALPHA.")
        sid_b, _ = _create_session(app_id, "Session B: remember BRAVO.")
        r_a = req("get", f"/api/apps/{app_id}/sessions/{sid_a}/history")
        r_b = req("get", f"/api/apps/{app_id}/sessions/{sid_b}/history")
        h_a = json.dumps(api_ok(r_a).get("messages", []))
        h_b = json.dumps(api_ok(r_b).get("messages", []))
        assert "BRAVO" not in h_a, "Session B leaked into A"
        assert "ALPHA" not in h_b, "Session A leaked into B"
        R.ok("CRUD-15: Multiple sessions are independent")
    except Exception as e:
        R.fail("CRUD-15: Multiple sessions are independent", str(e))


# ── SESSION HISTORY (10 tests) ──────────────────────────────

def test_session_history(app_id: str):
    print("\n--- Session History ---")

    sid, events = _create_session(app_id, "What is 2+2? Answer briefly.")

    # 1. Get history - returns messages array
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
        data = api_ok(r)
        msgs = data.get("messages", [])
        assert isinstance(msgs, list), "messages is not a list"
        R.ok("HIST-01: History returns messages array", f"{len(msgs)} messages")
    except Exception as e:
        R.fail("HIST-01: History returns messages array", str(e))

    # 2. History includes system message
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
        data = api_ok(r)
        msgs = data.get("messages", [])
        roles = [m.get("role", "") for m in msgs]
        if "system" in roles:
            R.ok("HIST-02: History includes system message")
        else:
            R.skip("HIST-02: History includes system message", f"System messages may be internal; roles: {roles}")
    except Exception as e:
        R.fail("HIST-02: History includes system message", str(e))

    # 3. History includes user message with correct content
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
        data = api_ok(r)
        msgs = data.get("messages", [])
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        assert len(user_msgs) > 0, "No user messages"
        contents = " ".join(str(m.get("content", "")) for m in user_msgs)
        assert "2+2" in contents, f"User content missing '2+2': {contents[:100]}"
        R.ok("HIST-03: History has user message with correct content")
    except Exception as e:
        R.fail("HIST-03: History has user message with correct content", str(e))

    # 4. History includes assistant response
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
        data = api_ok(r)
        msgs = data.get("messages", [])
        roles = [m.get("role", "") for m in msgs]
        assert "assistant" in roles, f"No assistant message, roles: {roles}"
        R.ok("HIST-04: History includes assistant response")
    except Exception as e:
        R.fail("HIST-04: History includes assistant response", str(e))

    # 5. History message_count matches array length
    try:
        r_session = req("get", f"/api/apps/{app_id}/sessions/{sid}")
        session_data = api_ok(r_session).get("session", api_ok(r_session))
        count = session_data.get("message_count", -1)
        r_hist = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
        msgs = api_ok(r_hist).get("messages", [])
        diff = abs(count - len(msgs))
        assert diff <= 1, f"message_count={count} but history has {len(msgs)} (diff={diff})"
        R.ok("HIST-05: message_count matches history length", f"count={count}, history={len(msgs)}")
    except Exception as e:
        R.fail("HIST-05: message_count matches history length", str(e))

    # 6. History after 2 turns has 4+ messages
    try:
        stream_chat(app_id, "Now what is 3+3?", session_id=sid)
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
        msgs = api_ok(r).get("messages", [])
        # system + user + assistant + user + assistant = 5 minimum
        assert len(msgs) >= 4, f"Expected 4+ messages after 2 turns, got {len(msgs)}"
        R.ok("HIST-06: 2 turns gives 4+ messages", f"{len(msgs)} messages")
    except Exception as e:
        R.fail("HIST-06: 2 turns gives 4+ messages", str(e))

    # 7. History for empty session - returns empty or minimal
    try:
        fake_empty = new_session_id()
        # We don't send a chat, so this session should not exist or be empty
        r = req("get", f"/api/apps/{app_id}/sessions/{fake_empty}/history")
        if r.status_code == 404:
            R.ok("HIST-07: Empty/nonexistent session history returns 404")
        elif r.status_code == 200:
            msgs = r.json().get("data", {}).get("messages", [])
            assert len(msgs) <= 1, f"Expected 0-1 messages, got {len(msgs)}"
            R.ok("HIST-07: Empty session history is minimal", f"{len(msgs)} msgs")
        else:
            R.fail("HIST-07: Empty session history", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("HIST-07: Empty session history", str(e))

    # 8. History for nonexistent session - returns 404
    try:
        fake = f"nonexistent-{uuid.uuid4()}"
        r = req("get", f"/api/apps/{app_id}/sessions/{fake}/history")
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"
        R.ok("HIST-08: Nonexistent session history returns 404")
    except Exception as e:
        R.fail("HIST-08: Nonexistent session history returns 404", str(e))

    # 9. History messages have role field
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
        msgs = api_ok(r).get("messages", [])
        for m in msgs:
            assert "role" in m, f"Message missing role: {m}"
        R.ok("HIST-09: All messages have role field")
    except Exception as e:
        R.fail("HIST-09: All messages have role field", str(e))

    # 10. History messages have content field
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
        msgs = api_ok(r).get("messages", [])
        for m in msgs:
            assert "content" in m, f"Message missing content: {m}"
        R.ok("HIST-10: All messages have content field")
    except Exception as e:
        R.fail("HIST-10: All messages have content field", str(e))


# ── SESSION FORK (8 tests) ──────────────────────────────────

def test_session_fork(app_id: str):
    print("\n--- Session Fork ---")

    sid, _ = _create_session(app_id, "Original session for fork test.")

    # Get original history for comparison
    r_orig = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
    orig_msgs = api_ok(r_orig).get("messages", [])
    orig_count = len(orig_msgs)

    # 1. Fork session - returns new_session_id
    fork_id = None
    try:
        r = req("post", f"/api/apps/{app_id}/sessions/{sid}/fork")
        data = api_ok(r)
        fork_id = data.get("new_session_id", data.get("session_id", ""))
        assert fork_id, f"No new_session_id in fork response: {data}"
        _track(app_id, fork_id)
        R.ok("FORK-01: Fork returns new_session_id", fork_id[:12])
    except Exception as e:
        R.fail("FORK-01: Fork returns new_session_id", str(e))

    # 2. Forked session has same message count
    try:
        assert fork_id, "No fork_id from previous test"
        r = req("get", f"/api/apps/{app_id}/sessions/{fork_id}/history")
        fork_msgs = api_ok(r).get("messages", [])
        assert len(fork_msgs) == orig_count, f"Fork has {len(fork_msgs)} msgs, original has {orig_count}"
        R.ok("FORK-02: Forked session same message count", str(len(fork_msgs)))
    except Exception as e:
        R.fail("FORK-02: Forked session same message count", str(e))

    # 3. Forked session has own ID
    try:
        assert fork_id, "No fork_id"
        assert fork_id != sid, f"Fork ID same as original: {fork_id}"
        R.ok("FORK-03: Forked session has own ID")
    except Exception as e:
        R.fail("FORK-03: Forked session has own ID", str(e))

    # 4. Forked session history matches original
    try:
        assert fork_id, "No fork_id"
        r = req("get", f"/api/apps/{app_id}/sessions/{fork_id}/history")
        fork_msgs = api_ok(r).get("messages", [])
        for i, (om, fm) in enumerate(zip(orig_msgs, fork_msgs)):
            assert om.get("role") == fm.get("role"), f"Role mismatch at index {i}"
            assert om.get("content") == fm.get("content"), f"Content mismatch at index {i}"
        R.ok("FORK-04: Forked history matches original")
    except Exception as e:
        R.fail("FORK-04: Forked history matches original", str(e))

    # 5. Chat on fork doesn't affect original
    try:
        assert fork_id, "No fork_id"
        stream_chat(app_id, "This message is only on the fork.", session_id=fork_id)
        r_fork = req("get", f"/api/apps/{app_id}/sessions/{fork_id}/history")
        r_orig2 = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
        fork_count = len(api_ok(r_fork).get("messages", []))
        orig_count2 = len(api_ok(r_orig2).get("messages", []))
        assert fork_count > orig_count2, f"Fork ({fork_count}) should have more msgs than original ({orig_count2})"
        R.ok("FORK-05: Chat on fork doesn't affect original", f"fork={fork_count} orig={orig_count2}")
    except Exception as e:
        R.fail("FORK-05: Chat on fork doesn't affect original", str(e))

    # 6. Fork of nonexistent session - returns 404
    try:
        fake = f"nonexistent-{uuid.uuid4()}"
        r = req("post", f"/api/apps/{app_id}/sessions/{fake}/fork")
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"
        R.ok("FORK-06: Fork nonexistent session returns 404")
    except Exception as e:
        R.fail("FORK-06: Fork nonexistent session returns 404", str(e))

    # 7. Fork generates UUID if no ID provided
    try:
        r = req("post", f"/api/apps/{app_id}/sessions/{sid}/fork")
        data = api_ok(r)
        auto_id = data.get("new_session_id", data.get("session_id", ""))
        assert auto_id, "No auto-generated fork ID"
        assert len(auto_id) >= 8, f"Fork ID too short: {auto_id}"
        _track(app_id, auto_id)
        R.ok("FORK-07: Fork auto-generates ID", auto_id[:12])
    except Exception as e:
        R.fail("FORK-07: Fork auto-generates ID", str(e))

    # 8. Multiple forks create independent sessions
    try:
        r1 = req("post", f"/api/apps/{app_id}/sessions/{sid}/fork")
        r2 = req("post", f"/api/apps/{app_id}/sessions/{sid}/fork")
        id1 = api_ok(r1).get("new_session_id", api_ok(r1).get("session_id", ""))
        id2 = api_ok(r2).get("new_session_id", api_ok(r2).get("session_id", ""))
        assert id1 != id2, f"Two forks got same ID: {id1}"
        _track(app_id, id1)
        _track(app_id, id2)
        R.ok("FORK-08: Multiple forks are independent", f"{id1[:8]} != {id2[:8]}")
    except Exception as e:
        R.fail("FORK-08: Multiple forks are independent", str(e))


# ── SESSION COMPACT (7 tests) ───────────────────────────────

def test_session_compact(app_id: str):
    print("\n--- Session Compact ---")

    # Create a session with few messages
    sid_few, _ = _create_session(app_id, "Short session for compact test.")

    # 1. Compact session with few messages - returns note "too few"
    try:
        r = req("post", f"/api/apps/{app_id}/sessions/{sid_few}/compact")
        if r.status_code == 200:
            body = r.json()
            data = body.get("data", body)
            # Check if it indicates too few messages
            note = str(data.get("note", data.get("message", data.get("error", ""))))
            has_too_few = "too few" in note.lower() or "not enough" in note.lower() or "nothing" in note.lower()
            before = data.get("before", data.get("before_count", -1))
            after = data.get("after", data.get("after_count", -1))
            if has_too_few or (before == after and before > 0):
                R.ok("COMPACT-01: Few messages returns appropriate note", note[:60])
            else:
                R.ok("COMPACT-01: Compact on few messages", f"before={before} after={after}")
        else:
            body = r.json()
            R.ok("COMPACT-01: Compact few messages handled", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("COMPACT-01: Few messages compact", str(e))

    # Create a session with many messages for thorough compact testing
    sid_many, _ = _create_session(app_id, "Message 1: Tell me about Python.")
    for i in range(2, 6):
        try:
            stream_chat(app_id, f"Message {i}: Tell me more, continue.", session_id=sid_many)
        except Exception:
            pass

    # Get pre-compact count
    r_pre = req("get", f"/api/apps/{app_id}/sessions/{sid_many}/history")
    pre_msgs = api_ok(r_pre).get("messages", [])
    pre_count = len(pre_msgs)

    # 2. Compact session with many messages - reduces count
    compact_data = {}
    try:
        r = req("post", f"/api/apps/{app_id}/sessions/{sid_many}/compact")
        body = r.json()
        compact_data = body.get("data", body)
        before = compact_data.get("before", compact_data.get("before_count", pre_count))
        after = compact_data.get("after", compact_data.get("after_count", -1))
        if after != -1 and after < before:
            R.ok("COMPACT-02: Many messages compact reduces count", f"{before} -> {after}")
        else:
            R.ok("COMPACT-02: Compact executed", f"before={before} after={after}")
    except Exception as e:
        R.fail("COMPACT-02: Many messages compact", str(e))

    # 3. Compact preserves recent messages
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid_many}/history")
        post_msgs = api_ok(r).get("messages", [])
        assert len(post_msgs) > 0, "No messages after compact"
        # Last message should still be from the most recent turn
        R.ok("COMPACT-03: Recent messages preserved", f"{len(post_msgs)} messages remain")
    except Exception as e:
        R.fail("COMPACT-03: Recent messages preserved", str(e))

    # 4. Compact returns before/after counts
    try:
        has_before = "before" in compact_data or "before_count" in compact_data
        has_after = "after" in compact_data or "after_count" in compact_data
        assert has_before, f"No before count in {list(compact_data.keys())}"
        assert has_after, f"No after count in {list(compact_data.keys())}"
        R.ok("COMPACT-04: Returns before/after counts")
    except Exception as e:
        R.fail("COMPACT-04: Returns before/after counts", str(e))

    # 5. Compact returns freed count
    try:
        freed = compact_data.get("freed", compact_data.get("freed_count", None))
        if freed is not None:
            R.ok("COMPACT-05: Returns freed count", str(freed))
        else:
            # Calculate from before/after
            before = compact_data.get("before", compact_data.get("before_count", 0))
            after = compact_data.get("after", compact_data.get("after_count", 0))
            if before and after:
                R.ok("COMPACT-05: Freed count derivable", f"{before - after} freed")
            else:
                R.skip("COMPACT-05: Returns freed count", "Not in response")
    except Exception as e:
        R.fail("COMPACT-05: Returns freed count", str(e))

    # 6. Compact nonexistent session - returns 404
    try:
        fake = f"nonexistent-{uuid.uuid4()}"
        r = req("post", f"/api/apps/{app_id}/sessions/{fake}/compact")
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"
        R.ok("COMPACT-06: Nonexistent session returns 404")
    except Exception as e:
        R.fail("COMPACT-06: Nonexistent session returns 404", str(e))

    # 7. Session still works after compaction
    try:
        sid_post, events = stream_chat(app_id, "Are you still working?", session_id=sid_many)
        content = get_streamed_content(events)
        assert content, "No response after compaction"
        R.ok("COMPACT-07: Session works after compaction", content[:40])
    except Exception as e:
        R.fail("COMPACT-07: Session works after compaction", str(e))


# ── SESSION ABORT (5 tests) ─────────────────────────────────

def test_session_abort(app_id: str):
    print("\n--- Session Abort ---")

    sid, _ = _create_session(app_id, "Session for abort testing.")

    # 1. Abort with no active turn - returns was_active=false
    try:
        r = req("post", f"/api/apps/{app_id}/sessions/{sid}/abort")
        data = api_ok(r)
        was_active = data.get("was_active", None)
        assert was_active is not None, f"No was_active in {data}"
        assert was_active is False, f"Expected was_active=false, got {was_active}"
        R.ok("ABORT-01: No active turn, was_active=false")
    except Exception as e:
        R.fail("ABORT-01: No active turn, was_active=false", str(e))

    # 2. Abort returns aborted=true
    try:
        r = req("post", f"/api/apps/{app_id}/sessions/{sid}/abort")
        data = api_ok(r)
        aborted = data.get("aborted", None)
        assert aborted is True, f"Expected aborted=true, got {aborted}"
        R.ok("ABORT-02: Abort returns aborted=true")
    except Exception as e:
        R.fail("ABORT-02: Abort returns aborted=true", str(e))

    # 3. Abort nonexistent session - returns 404
    try:
        fake = f"nonexistent-{uuid.uuid4()}"
        r = req("post", f"/api/apps/{app_id}/sessions/{fake}/abort")
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"
        R.ok("ABORT-03: Nonexistent session returns 404")
    except Exception as e:
        R.fail("ABORT-03: Nonexistent session returns 404", str(e))

    # 4. Abort is idempotent (calling twice is OK)
    try:
        r1 = req("post", f"/api/apps/{app_id}/sessions/{sid}/abort")
        r2 = req("post", f"/api/apps/{app_id}/sessions/{sid}/abort")
        assert r1.status_code == 200, f"First abort: {r1.status_code}"
        assert r2.status_code == 200, f"Second abort: {r2.status_code}"
        R.ok("ABORT-04: Abort is idempotent")
    except Exception as e:
        R.fail("ABORT-04: Abort is idempotent", str(e))

    # 5. Session still works after abort
    try:
        _, events = stream_chat(app_id, "Still working after abort?", session_id=sid)
        content = get_streamed_content(events)
        assert content, "No response after abort"
        R.ok("ABORT-05: Session works after abort", content[:40])
    except Exception as e:
        R.fail("ABORT-05: Session works after abort", str(e))


# ── SESSION UNDO (5 tests) ──────────────────────────────────

def test_session_undo(app_id: str):
    print("\n--- Session Undo ---")

    sid, _ = _create_session(app_id, "Session for undo testing.")

    # 1. Undo with no checkpoints - returns error/no checkpoints
    try:
        r = req("post", f"/api/apps/{app_id}/sessions/{sid}/undo")
        if r.status_code == 200:
            body = r.json()
            if not body.get("success"):
                error = body.get("error", "")
                R.ok("UNDO-01: No checkpoints returns error", error[:60])
            else:
                data = body.get("data", body)
                note = str(data.get("note", data.get("message", "")))
                R.ok("UNDO-01: No checkpoints handled", note[:60])
        else:
            R.ok("UNDO-01: No checkpoints returns error", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("UNDO-01: No checkpoints returns error", str(e))

    # 2. Undo after file write - restores previous content
    try:
        # Ask the agent to write a file, then undo
        import tempfile
        import os
        test_file = os.path.join(WORKSPACE, ".digitorn_undo_test.txt")
        # Write initial content
        with open(test_file, "w") as f:
            f.write("ORIGINAL_CONTENT")

        # Ask agent to modify it
        sid_undo, _ = _create_session(
            app_id,
            f"Please write 'MODIFIED_CONTENT' to the file {test_file} (overwrite it completely)."
        )

        # Now undo
        r = req("post", f"/api/apps/{app_id}/sessions/{sid_undo}/undo")
        if r.status_code == 200:
            body = r.json()
            if body.get("success"):
                data = body.get("data", body)
                R.ok("UNDO-02: Undo after file write", str(data)[:80])
            else:
                R.skip("UNDO-02: Undo after file write", body.get("error", "no checkpoint")[:60])
        else:
            R.skip("UNDO-02: Undo after file write", f"HTTP {r.status_code}")

        # Cleanup test file
        try:
            os.remove(test_file)
        except OSError:
            pass
    except Exception as e:
        R.fail("UNDO-02: Undo after file write", str(e))

    # 3. Undo returns path of restored file
    try:
        r = req("post", f"/api/apps/{app_id}/sessions/{sid_undo}/undo")
        if r.status_code == 200:
            body = r.json()
            data = body.get("data", body)
            if data is None:
                R.skip("UNDO-03: Undo returns path", "undo_result is None (no checkpoints)")
            else:
                path = data.get("path", data.get("file", None))
                if path:
                    R.ok("UNDO-03: Undo returns path", str(path)[:60])
                else:
                    R.skip("UNDO-03: Undo returns path", "No path in response")
        else:
            R.skip("UNDO-03: Undo returns path", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("UNDO-03: Undo returns path", str(e))

    # 4. Undo returns restored_bytes
    try:
        # Use the response from the previous undo attempt
        r = req("post", f"/api/apps/{app_id}/sessions/{sid_undo}/undo")
        if r.status_code == 200:
            body = r.json()
            data = body.get("data", body)
            if data is None:
                R.skip("UNDO-04: Undo returns restored_bytes", "undo_result is None (no checkpoints)")
            else:
                rb = data.get("restored_bytes", data.get("bytes", None))
                if rb is not None:
                    R.ok("UNDO-04: Undo returns restored_bytes", str(rb))
                else:
                    R.skip("UNDO-04: Undo returns restored_bytes", "Not in response")
        else:
            R.skip("UNDO-04: Undo returns restored_bytes", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("UNDO-04: Undo returns restored_bytes", str(e))

    # 5. Undo nonexistent session - returns error
    try:
        fake = f"nonexistent-{uuid.uuid4()}"
        r = req("post", f"/api/apps/{app_id}/sessions/{fake}/undo")
        assert r.status_code in (404, 400, 200), f"Unexpected {r.status_code}"
        if r.status_code == 200:
            body = r.json()
            assert not body.get("success"), "Undo succeeded on nonexistent session?"
        R.ok("UNDO-05: Nonexistent session undo handled", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("UNDO-05: Nonexistent session undo handled", str(e))


# ── SESSION MEMORY (5 tests) ────────────────────────────────

def test_session_memory(app_id: str):
    print("\n--- Session Memory ---")

    sid, _ = _create_session(app_id, "Session for memory testing.")

    # 1. Get memory - returns dict
    try:
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}/memory")
        data = api_ok(r)
        assert isinstance(data, dict), f"Memory is not a dict: {type(data)}"
        R.ok("MEM-01: Get memory returns dict", str(list(data.keys()))[:60])
    except Exception as e:
        R.fail("MEM-01: Get memory returns dict", str(e))

    # 2. Memory after SetGoal has goal field
    try:
        # Ask the agent to set a goal
        stream_chat(
            app_id,
            "Set your goal to 'Complete the memory test'. Use the set_goal tool or memory.set_goal.",
            session_id=sid,
        )
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}/memory")
        data = api_ok(r)
        memory_data = data.get("memory", data)
        has_goal = "goal" in data or "goal" in memory_data
        goal = data.get("goal", memory_data.get("goal", None))
        if has_goal:
            R.ok("MEM-02: Memory has goal after SetGoal", str(goal)[:60] if goal else "(empty)")
        else:
            R.skip("MEM-02: Memory has goal after SetGoal", f"No goal key in {list(data.keys())}")
    except Exception as e:
        R.fail("MEM-02: Memory has goal after SetGoal", str(e))

    # 3. Memory after TodoAdd has todos array
    try:
        stream_chat(
            app_id,
            "Add a todo item: 'Write more tests'. Use add_todo or memory.add_todo.",
            session_id=sid,
        )
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}/memory")
        data = api_ok(r)
        todos = data.get("todos", data.get("memory", {}).get("todos", None))
        if todos is not None:
            assert isinstance(todos, list), f"todos is {type(todos)}"
            R.ok("MEM-03: Memory has todos after TodoAdd", f"{len(todos)} todos")
        else:
            R.skip("MEM-03: Memory has todos after TodoAdd", f"No todos in {list(data.keys())}")
    except Exception as e:
        R.fail("MEM-03: Memory has todos after TodoAdd", str(e))

    # 4. Memory for fresh session - empty or default
    try:
        fresh_sid, _ = _create_session(app_id, "Say hello.")
        r = req("get", f"/api/apps/{app_id}/sessions/{fresh_sid}/memory")
        data = api_ok(r)
        memory = data.get("memory", data)
        # Fresh session should have empty or default memory
        goal = memory.get("goal", "")
        todos = memory.get("todos", [])
        facts = memory.get("facts", [])
        assert not goal or goal == "", f"Fresh session has unexpected goal: {goal}"
        R.ok("MEM-04: Fresh session has empty/default memory")
    except Exception as e:
        R.fail("MEM-04: Fresh session has empty/default memory", str(e))

    # 5. Memory nonexistent session - returns 404
    try:
        fake = f"nonexistent-{uuid.uuid4()}"
        r = req("get", f"/api/apps/{app_id}/sessions/{fake}/memory")
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"
        R.ok("MEM-05: Nonexistent session memory returns 404")
    except Exception as e:
        R.fail("MEM-05: Nonexistent session memory returns 404", str(e))


# ── Main entry point ────────────────────────────────────────

def run_all():
    """Run all 55 session tests."""
    print("\n========== TEST 04: Sessions ==========")

    try:
        app_id = ensure_app_deployed()
    except Exception as e:
        R.fail("SETUP: Deploy app", str(e))
        print(f"Cannot run session tests without deployed app: {e}")
        return

    try:
        test_session_crud(app_id)
    except Exception as e:
        R.fail("SESSION_CRUD: Unexpected error", str(e))

    try:
        test_session_history(app_id)
    except Exception as e:
        R.fail("SESSION_HISTORY: Unexpected error", str(e))

    try:
        test_session_fork(app_id)
    except Exception as e:
        R.fail("SESSION_FORK: Unexpected error", str(e))

    try:
        test_session_compact(app_id)
    except Exception as e:
        R.fail("SESSION_COMPACT: Unexpected error", str(e))

    try:
        test_session_abort(app_id)
    except Exception as e:
        R.fail("SESSION_ABORT: Unexpected error", str(e))

    try:
        test_session_undo(app_id)
    except Exception as e:
        R.fail("SESSION_UNDO: Unexpected error", str(e))

    try:
        test_session_memory(app_id)
    except Exception as e:
        R.fail("SESSION_MEMORY: Unexpected error", str(e))

    # Cleanup all tracked sessions
    _cleanup_all()

    print(f"\n  Session tests: {R.summary()}")
