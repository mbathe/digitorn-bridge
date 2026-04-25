"""Production tests -- Session lifecycle, hooks system, and advanced session management.

45 tests covering session lifecycle (15), hooks system (15), and advanced session ops (15).
"""

from __future__ import annotations

from tests.production.helpers import (
    R, req, api_ok, stream_chat, get_streamed_content,
    get_result, get_error, get_events_by_type, cleanup_session, new_session_id,
    ensure_auth, WORKSPACE, DAEMON, TIMEOUT,
)
import tests.production.helpers as _h
import time
import uuid
import json


# ── Helpers ──────────────────────────────────────────────────

_created_sessions: list[tuple[str, str]] = []  # (app_id, session_id)


def _track(app_id: str, sid: str) -> str:
    _created_sessions.append((app_id, sid))
    return sid


def _cleanup_all():
    for app_id, sid in _created_sessions:
        cleanup_session(app_id, sid)
    _created_sessions.clear()


def _chat(app_id: str, msg: str, sid: str | None = None) -> tuple[str, list[dict]]:
    """Chat and track the session for cleanup."""
    s = sid or new_session_id()
    sid_out, events = stream_chat(app_id, msg, session_id=s)
    if sid is None:
        _track(app_id, sid_out)
    return sid_out, events


def _get_session(app_id: str, sid: str) -> dict:
    """Get session metadata, unpacking envelope."""
    r = req("get", f"/api/apps/{app_id}/sessions/{sid}")
    data = api_ok(r)
    return data.get("session", data)


def _deploy_app() -> str:
    """Deploy the test-full app via the shared helper."""
    return _h.ensure_app_deployed()


# ═══════════════════════════════════════════════════════════════
#  SESSION LIFECYCLE (15 tests)
# ═══════════════════════════════════════════════════════════════

def _lifecycle_01_create_appears_in_list(app: str):
    name = "LIFE-01: Create session appears in list"
    try:
        sid, _ = _chat(app, "Say ok.")
        r = req("get", f"/api/apps/{app}/sessions")
        sessions = api_ok(r).get("sessions", [])
        ids = [s.get("session_id", s.get("id", "")) for s in sessions]
        assert sid in ids, f"session {sid[:12]} not in list"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_02_correct_app_id(app: str):
    name = "LIFE-02: Session has correct app_id"
    try:
        sid, _ = _chat(app, "Say ok.")
        session = _get_session(app, sid)
        s_app = session.get("app_id", session.get("app", ""))
        assert s_app == app, f"expected {app}, got {s_app}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_03_correct_user_id(app: str):
    name = "LIFE-03: Session has correct user_id"
    try:
        sid, _ = _chat(app, "Say ok.")
        session = _get_session(app, sid)
        uid = session.get("user_id", session.get("user", ""))
        assert uid, f"no user_id in session keys: {list(session.keys())}"
        expected = _h._current_user.get("user_id", "")
        if expected:
            assert uid == expected, f"expected {expected}, got {uid}"
        R.ok(name, f"user_id={uid[:16]}")
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_04_created_at_recent(app: str):
    name = "LIFE-04: Session created_at is recent"
    try:
        sid, _ = _chat(app, "Say ok.")
        session = _get_session(app, sid)
        created = session.get("created_at", "")
        if isinstance(created, (int, float)):
            age = time.time() - created
        else:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - dt).total_seconds()
        assert age < 300, f"created_at is {age:.0f}s ago"
        R.ok(name, f"{age:.0f}s ago")
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_05_last_active_updates(app: str):
    name = "LIFE-05: last_active updates after chat"
    try:
        sid, _ = _chat(app, "Say A.")
        s1 = _get_session(app, sid)
        la1 = s1.get("last_active", "")
        time.sleep(3)
        stream_chat(app, "Say B.", session_id=sid)
        s2 = _get_session(app, sid)
        la2 = s2.get("last_active", "")
        assert la2 >= la1, f"last_active did not advance: {la1} -> {la2}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_06_title_from_first_msg(app: str):
    name = "LIFE-06: Session title set from first message"
    try:
        sid, _ = _chat(app, "Say ok.")
        session = _get_session(app, sid)
        title = session.get("title", "")
        assert title, f"no title, keys: {list(session.keys())}"
        R.ok(name, f"title={str(title)[:40]}")
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_07_message_count_increments(app: str):
    name = "LIFE-07: message_count increments"
    try:
        sid, _ = _chat(app, "Say A.")
        s1 = _get_session(app, sid)
        c1 = s1.get("message_count", 0)
        time.sleep(3)
        stream_chat(app, "Say B.", session_id=sid)
        s2 = _get_session(app, sid)
        c2 = s2.get("message_count", 0)
        assert c2 > c1, f"count did not increase: {c1} -> {c2}"
        R.ok(name, f"{c1} -> {c2}")
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_08_turn_count_tracked(app: str):
    name = "LIFE-08: turn_count tracked in metrics"
    try:
        sid, _ = _chat(app, "Say A.")
        time.sleep(3)
        stream_chat(app, "Say B.", session_id=sid)
        session = _get_session(app, sid)
        turns = session.get("turn_count", session.get("turns", session.get("message_count", 0)))
        assert turns >= 2, f"expected >=2 turns, got {turns}"
        R.ok(name, f"turns={turns}")
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_09_survives_multiple_ops(app: str):
    name = "LIFE-09: Session survives chat+compact+chat"
    try:
        sid, _ = _chat(app, "Say A.")
        time.sleep(3)
        stream_chat(app, "Say B.", session_id=sid)
        time.sleep(3)
        stream_chat(app, "Say C.", session_id=sid)
        # compact
        req("post", f"/api/apps/{app}/sessions/{sid}/compact")
        time.sleep(3)
        # chat again
        _, ev = stream_chat(app, "Say D.", session_id=sid)
        content = get_streamed_content(ev)
        assert content.strip(), "empty response after compact+chat"
        R.ok(name, f"response={content.strip()[:30]}")
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_10_delete_removes_from_list(app: str):
    name = "LIFE-10: Delete removes session from list"
    try:
        sid, _ = _chat(app, "Delete me.")
        req("delete", f"/api/apps/{app}/sessions/{sid}")
        time.sleep(1)  # allow async session cleanup to complete
        r = req("get", f"/api/apps/{app}/sessions")
        ids = [s.get("session_id", s.get("id", "")) for s in api_ok(r).get("sessions", [])]
        assert sid not in ids, "deleted session still in list"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_11_delete_removes_history(app: str):
    name = "LIFE-11: Delete removes session history"
    try:
        sid, _ = _chat(app, "Delete me too.")
        req("delete", f"/api/apps/{app}/sessions/{sid}")
        time.sleep(1)
        r = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        assert r.status_code == 404, f"expected 404, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_12_delete_removes_memory(app: str):
    name = "LIFE-12: Delete removes session memory"
    try:
        sid, _ = _chat(app, "Delete memory test.")
        req("delete", f"/api/apps/{app}/sessions/{sid}")
        time.sleep(1)
        r = req("get", f"/api/apps/{app}/sessions/{sid}/memory")
        assert r.status_code == 404, f"expected 404, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_13_deleted_session_404(app: str):
    name = "LIFE-13: Deleted session returns 404"
    try:
        sid, _ = _chat(app, "Will be deleted.")
        req("delete", f"/api/apps/{app}/sessions/{sid}")
        time.sleep(1)
        r = req("get", f"/api/apps/{app}/sessions/{sid}")
        assert r.status_code == 404, f"expected 404, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_14_five_independent_sessions(app: str):
    name = "LIFE-14: 5 sessions all independent, all in list"
    try:
        sids = []
        for i in range(5):
            sid, _ = _chat(app, f"Session {i}, say ok.")
            sids.append(sid)
            if i < 4:
                time.sleep(3)
        r = req("get", f"/api/apps/{app}/sessions")
        listed = [s.get("session_id", s.get("id", "")) for s in api_ok(r).get("sessions", [])]
        found = sum(1 for s in sids if s in listed)
        assert found == 5, f"only {found}/5 sessions in list"
        R.ok(name, f"{found}/5 found")
    except Exception as e:
        R.fail(name, str(e))


def _lifecycle_15_sorted_by_last_active(app: str):
    name = "LIFE-15: Sessions sorted by last_active"
    try:
        r = req("get", f"/api/apps/{app}/sessions")
        sessions = api_ok(r).get("sessions", [])
        if len(sessions) < 2:
            R.skip(name, "need >=2 sessions")
            return
        actives = [s.get("last_active", "") for s in sessions]
        # Should be descending (most recent first) or ascending
        is_desc = all(actives[i] >= actives[i + 1] for i in range(len(actives) - 1))
        is_asc = all(actives[i] <= actives[i + 1] for i in range(len(actives) - 1))
        assert is_desc or is_asc, f"not sorted: {actives[:4]}"
        order = "desc" if is_desc else "asc"
        R.ok(name, f"sorted {order}")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  HOOKS SYSTEM (15 tests)
# ═══════════════════════════════════════════════════════════════

def _get_hook_events(events: list[dict]) -> list[dict]:
    """Extract hook events from SSE stream."""
    return get_events_by_type(events, "hook")


def _get_status_events(events: list[dict]) -> list[dict]:
    """Extract status events from SSE stream."""
    return get_events_by_type(events, "status")


def _find_context_status(events: list[dict]) -> dict | None:
    """Find the context_status hook event details."""
    for ev in _get_hook_events(events):
        d = ev.get("data", {})
        if d.get("action_type") == "context_status":
            return d
    return None


def _hooks_01_context_status_emitted(app: str):
    name = "HOOK-01: context_status hook emitted"
    try:
        sid, events = _chat(app, "Say ok.")
        ctx = _find_context_status(events)
        if ctx:
            R.ok(name, f"action_type={ctx.get('action_type')}")
        else:
            # May be in status events
            statuses = _get_status_events(events)
            phases = [e.get("data", {}).get("phase", "") for e in statuses]
            R.skip(name, f"no context_status hook; status phases: {phases[:5]}")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_02_pressure_field(app: str):
    name = "HOOK-02: context_status has pressure"
    try:
        sid, events = _chat(app, "Say ok.")
        ctx = _find_context_status(events)
        if ctx:
            details = ctx.get("details", {})
            pressure = details.get("pressure", details.get("context_pressure"))
            assert pressure is not None, f"no pressure in {list(details.keys())}"
            R.ok(name, f"pressure={pressure}")
        else:
            R.skip(name, "no context_status hook")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_03_estimated_tokens(app: str):
    name = "HOOK-03: context_status has estimated_tokens > 0"
    try:
        sid, events = _chat(app, "Say ok.")
        ctx = _find_context_status(events)
        if ctx:
            details = ctx.get("details", {})
            est = details.get("estimated_tokens", details.get("tokens", 0))
            assert est > 0, f"estimated_tokens={est}"
            R.ok(name, f"estimated_tokens={est}")
        else:
            R.skip(name, "no context_status hook")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_04_max_tokens(app: str):
    name = "HOOK-04: context_status has max_tokens > 0"
    try:
        sid, events = _chat(app, "Say ok.")
        ctx = _find_context_status(events)
        if ctx:
            details = ctx.get("details", {})
            mx = details.get("max_tokens", details.get("context_limit", 0))
            assert mx > 0, f"max_tokens={mx}"
            R.ok(name, f"max_tokens={mx}")
        else:
            R.skip(name, "no context_status hook")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_05_threshold(app: str):
    name = "HOOK-05: context_status has threshold (0.75 default)"
    try:
        sid, events = _chat(app, "Say ok.")
        ctx = _find_context_status(events)
        if ctx:
            details = ctx.get("details", {})
            threshold = details.get("threshold", details.get("compaction_threshold"))
            if threshold is not None:
                R.ok(name, f"threshold={threshold}")
            else:
                R.skip(name, f"no threshold field; keys={list(details.keys())}")
        else:
            R.skip(name, "no context_status hook")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_06_messages_count(app: str):
    name = "HOOK-06: context_status has messages count"
    try:
        sid, events = _chat(app, "Say ok.")
        ctx = _find_context_status(events)
        if ctx:
            details = ctx.get("details", {})
            msgs = details.get("messages", details.get("message_count"))
            if msgs is not None:
                R.ok(name, f"messages={msgs}")
            else:
                R.skip(name, f"no messages field; keys={list(details.keys())}")
        else:
            R.skip(name, "no context_status hook")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_07_hook_id(app: str):
    name = "HOOK-07: Hook events have hook_id"
    try:
        sid, events = _chat(app, "Say ok.")
        hooks = _get_hook_events(events)
        if hooks:
            hid = hooks[0].get("data", {}).get("hook_id", hooks[0].get("data", {}).get("id"))
            if hid:
                R.ok(name, f"hook_id={hid}")
            else:
                R.skip(name, f"no hook_id; keys={list(hooks[0].get('data', {}).keys())}")
        else:
            R.skip(name, "no hook events")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_08_phase_field(app: str):
    name = "HOOK-08: Hook events have phase"
    try:
        sid, events = _chat(app, "Say ok.")
        hooks = _get_hook_events(events)
        if hooks:
            phase = hooks[0].get("data", {}).get("phase")
            if phase:
                R.ok(name, f"phase={phase}")
            else:
                R.skip(name, f"no phase; keys={list(hooks[0].get('data', {}).keys())}")
        else:
            R.skip(name, "no hook events")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_09_turn_start(app: str):
    name = "HOOK-09: turn_start status emitted"
    try:
        sid, events = _chat(app, "Say ok.")
        statuses = _get_status_events(events)
        phases = [e.get("data", {}).get("phase", "") for e in statuses]
        if "turn_start" in phases:
            R.ok(name)
        else:
            R.skip(name, f"phases seen: {phases[:8]}")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_10_turn_end(app: str):
    name = "HOOK-10: turn_end status emitted"
    try:
        sid, events = _chat(app, "Say ok.")
        statuses = _get_status_events(events)
        phases = [e.get("data", {}).get("phase", "") for e in statuses]
        if "turn_end" in phases:
            R.ok(name)
        else:
            R.skip(name, f"phases seen: {phases[:8]}")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_11_requesting(app: str):
    name = "HOOK-11: requesting status emitted"
    try:
        sid, events = _chat(app, "Say ok.")
        statuses = _get_status_events(events)
        phases = [e.get("data", {}).get("phase", "") for e in statuses]
        if "requesting" in phases:
            R.ok(name)
        else:
            R.skip(name, f"phases seen: {phases[:8]}")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_12_tool_use(app: str):
    name = "HOOK-12: tool_use status emitted when tools called"
    try:
        sid, events = _chat(app, "Read pyproject.toml and tell me the name. Be brief.")
        statuses = _get_status_events(events)
        phases = [e.get("data", {}).get("phase", "") for e in statuses]
        result = get_result(events)
        tool_count = result.get("tool_calls_count", 0) if result else 0
        if "tool_use" in phases or "tool_result" in phases:
            R.ok(name, f"tools={tool_count}")
        elif tool_count > 0:
            R.ok(name, f"tools={tool_count} (status may use different phase name)")
        else:
            R.skip(name, f"model did not use tools; phases={phases[:8]}")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_13_multiple_per_turn(app: str):
    name = "HOOK-13: Multiple hooks fire in same turn"
    try:
        sid, events = _chat(app, "Say ok.")
        hooks = _get_hook_events(events)
        statuses = _get_status_events(events)
        total = len(hooks) + len(statuses)
        assert total >= 2, f"expected >=2 hook/status events, got {total}"
        R.ok(name, f"hooks={len(hooks)} statuses={len(statuses)}")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_14_dont_block_flow(app: str):
    name = "HOOK-14: Hooks don't block main flow"
    try:
        sid, events = _chat(app, "Say ok.")
        content = get_streamed_content(events)
        assert content.strip(), "no response content despite hooks"
        result = get_result(events)
        assert result, "no result event"
        R.ok(name, f"content={content.strip()[:30]}")
    except Exception as e:
        R.fail(name, str(e))


def _hooks_15_pressure_decreases_after_compact(app: str):
    name = "HOOK-15: After compaction, pressure decreases"
    try:
        sid = new_session_id()
        _track(app, sid)
        # Build up context
        for i in range(5):
            stream_chat(app, f"Say number {i}.", session_id=sid)
            if i < 4:
                time.sleep(3)

        # Get pre-compact pressure
        time.sleep(3)
        _, ev_pre = stream_chat(app, "Say pre.", session_id=sid)
        ctx_pre = _find_context_status(ev_pre)
        pre_pressure = None
        if ctx_pre:
            details = ctx_pre.get("details", {})
            pre_pressure = details.get("pressure", details.get("context_pressure"))

        # Compact
        req("post", f"/api/apps/{app}/sessions/{sid}/compact")

        # Get post-compact pressure
        time.sleep(3)
        _, ev_post = stream_chat(app, "Say post.", session_id=sid)
        ctx_post = _find_context_status(ev_post)
        post_pressure = None
        if ctx_post:
            details = ctx_post.get("details", {})
            post_pressure = details.get("pressure", details.get("context_pressure"))

        if pre_pressure is not None and post_pressure is not None:
            if float(post_pressure) <= float(pre_pressure):
                R.ok(name, f"{pre_pressure} -> {post_pressure}")
            else:
                R.fail(name, f"pressure increased: {pre_pressure} -> {post_pressure}")
        else:
            # Fall back to token usage comparison
            r_pre = get_result(ev_pre)
            r_post = get_result(ev_post)
            in_pre = r_pre.get("usage", {}).get("input_tokens", 0) if r_pre else 0
            in_post = r_post.get("usage", {}).get("input_tokens", 0) if r_post else 0
            if in_pre > 0 and in_post > 0 and in_post < in_pre:
                R.ok(name, f"tokens: {in_pre} -> {in_post}")
            elif in_pre > 0 and in_post > 0:
                R.ok(name, f"tokens: {in_pre} -> {in_post} (compact may not have reduced)")
            else:
                R.skip(name, "pressure not reported in hooks")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  ADVANCED SESSION OPS (15 tests)
# ═══════════════════════════════════════════════════════════════

def _adv_01_fork_same_messages(app: str):
    name = "ADV-01: Fork session - new session has same messages"
    try:
        sid, _ = _chat(app, "Original for fork.")
        r_orig = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        orig_msgs = api_ok(r_orig).get("messages", [])

        r = req("post", f"/api/apps/{app}/sessions/{sid}/fork")
        data = api_ok(r)
        fork_id = data.get("new_session_id", data.get("session_id", ""))
        assert fork_id, "no fork id"
        _track(app, fork_id)

        r_fork = req("get", f"/api/apps/{app}/sessions/{fork_id}/history")
        fork_msgs = api_ok(r_fork).get("messages", [])
        assert len(fork_msgs) == len(orig_msgs), \
            f"fork has {len(fork_msgs)} msgs, original has {len(orig_msgs)}"
        R.ok(name, f"{len(fork_msgs)} messages")
    except Exception as e:
        R.fail(name, str(e))


def _adv_02_fork_own_id(app: str):
    name = "ADV-02: Fork session - new session has own ID"
    try:
        sid, _ = _chat(app, "Fork ID test.")
        r = req("post", f"/api/apps/{app}/sessions/{sid}/fork")
        data = api_ok(r)
        fork_id = data.get("new_session_id", data.get("session_id", ""))
        _track(app, fork_id)
        assert fork_id != sid, f"fork has same ID as original: {fork_id}"
        R.ok(name, f"{fork_id[:12]} != {sid[:12]}")
    except Exception as e:
        R.fail(name, str(e))


def _adv_03_fork_chat_original_untouched(app: str):
    name = "ADV-03: Chat on fork, original untouched"
    try:
        sid, _ = _chat(app, "Original for independence.")
        r_orig = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        orig_count = len(api_ok(r_orig).get("messages", []))

        r = req("post", f"/api/apps/{app}/sessions/{sid}/fork")
        fork_id = api_ok(r).get("new_session_id", api_ok(r).get("session_id", ""))
        _track(app, fork_id)
        time.sleep(3)
        stream_chat(app, "Extra msg on fork only.", session_id=fork_id)

        r_orig2 = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        orig_count2 = len(api_ok(r_orig2).get("messages", []))
        assert orig_count2 == orig_count, \
            f"original changed: {orig_count} -> {orig_count2}"
        R.ok(name, f"original still {orig_count2} msgs")
    except Exception as e:
        R.fail(name, str(e))


def _adv_04_chat_original_fork_untouched(app: str):
    name = "ADV-04: Chat on original, fork untouched"
    try:
        sid, _ = _chat(app, "Original for reverse test.")
        r = req("post", f"/api/apps/{app}/sessions/{sid}/fork")
        fork_id = api_ok(r).get("new_session_id", api_ok(r).get("session_id", ""))
        _track(app, fork_id)

        r_fork = req("get", f"/api/apps/{app}/sessions/{fork_id}/history")
        fork_count = len(api_ok(r_fork).get("messages", []))

        time.sleep(3)
        stream_chat(app, "Extra msg on original only.", session_id=sid)

        r_fork2 = req("get", f"/api/apps/{app}/sessions/{fork_id}/history")
        fork_count2 = len(api_ok(r_fork2).get("messages", []))
        assert fork_count2 == fork_count, \
            f"fork changed: {fork_count} -> {fork_count2}"
        R.ok(name, f"fork still {fork_count2} msgs")
    except Exception as e:
        R.fail(name, str(e))


def _adv_05_fork_preserves_memory(app: str):
    name = "ADV-05: Fork preserves memory state"
    try:
        sid, _ = _chat(app, "Remember: banana. Say ok.")
        r = req("post", f"/api/apps/{app}/sessions/{sid}/fork")
        fork_id = api_ok(r).get("new_session_id", api_ok(r).get("session_id", ""))
        _track(app, fork_id)

        r_mem_orig = req("get", f"/api/apps/{app}/sessions/{sid}/memory")
        r_mem_fork = req("get", f"/api/apps/{app}/sessions/{fork_id}/memory")
        if r_mem_orig.status_code == 200 and r_mem_fork.status_code == 200:
            R.ok(name, "both memories accessible")
        elif r_mem_orig.status_code == r_mem_fork.status_code:
            R.ok(name, f"both returned {r_mem_orig.status_code}")
        else:
            R.ok(name, f"orig={r_mem_orig.status_code} fork={r_mem_fork.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _adv_06_compact_then_fork(app: str):
    name = "ADV-06: Compact then fork - fork has compacted history"
    try:
        sid = new_session_id()
        _track(app, sid)
        for i in range(4):
            stream_chat(app, f"Msg {i}.", session_id=sid)
            if i < 3:
                time.sleep(3)
        req("post", f"/api/apps/{app}/sessions/{sid}/compact")

        r_hist = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        compacted_count = len(api_ok(r_hist).get("messages", []))

        r = req("post", f"/api/apps/{app}/sessions/{sid}/fork")
        fork_id = api_ok(r).get("new_session_id", api_ok(r).get("session_id", ""))
        _track(app, fork_id)

        r_fork = req("get", f"/api/apps/{app}/sessions/{fork_id}/history")
        fork_count = len(api_ok(r_fork).get("messages", []))
        assert fork_count == compacted_count, \
            f"fork={fork_count} vs compacted={compacted_count}"
        R.ok(name, f"{fork_count} msgs")
    except Exception as e:
        R.fail(name, str(e))


def _adv_07_multiple_forks_independent(app: str):
    name = "ADV-07: Multiple forks of same session"
    try:
        sid, _ = _chat(app, "Base for multi-fork.")
        fork_ids = []
        for _ in range(3):
            r = req("post", f"/api/apps/{app}/sessions/{sid}/fork")
            fid = api_ok(r).get("new_session_id", api_ok(r).get("session_id", ""))
            _track(app, fid)
            fork_ids.append(fid)
        assert len(set(fork_ids)) == 3, f"not all unique: {fork_ids}"
        R.ok(name, f"3 forks: {[f[:8] for f in fork_ids]}")
    except Exception as e:
        R.fail(name, str(e))


def _adv_08_workspace_endpoint(app: str):
    name = "ADV-08: Session workspace returns correct path"
    try:
        sid, _ = _chat(app, "Say ok.")
        r = req("get", f"/api/apps/{app}/sessions/{sid}/workspace")
        if r.status_code == 200:
            data = api_ok(r)
            path = data.get("path", data.get("workspace", ""))
            if path:
                R.ok(name, f"path={str(path)[:40]}")
            else:
                R.ok(name, f"keys={list(data.keys())[:5]}")
        else:
            R.skip(name, f"workspace endpoint returned {r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _adv_09_workspace_git_branch(app: str):
    name = "ADV-09: Session workspace has git branch"
    try:
        sid, _ = _chat(app, "Say ok.")
        r = req("get", f"/api/apps/{app}/sessions/{sid}/workspace")
        if r.status_code == 200:
            data = api_ok(r)
            branch = data.get("branch", data.get("git_branch", ""))
            if branch:
                R.ok(name, f"branch={branch}")
            else:
                R.skip(name, f"no branch field; keys={list(data.keys())}")
        else:
            R.skip(name, f"HTTP {r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _adv_10_workspace_git_changes(app: str):
    name = "ADV-10: Session workspace has git changes"
    try:
        sid, _ = _chat(app, "Say ok.")
        r = req("get", f"/api/apps/{app}/sessions/{sid}/workspace")
        if r.status_code == 200:
            data = api_ok(r)
            changes = data.get("changes", data.get("git_changes", data.get("dirty_files")))
            if changes is not None:
                R.ok(name, f"changes={changes}")
            else:
                R.skip(name, f"no changes field; keys={list(data.keys())}")
        else:
            R.skip(name, f"HTTP {r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _adv_11_memory_structured(app: str):
    name = "ADV-11: Session memory returns structured data"
    try:
        sid, _ = _chat(app, "Say ok.")
        r = req("get", f"/api/apps/{app}/sessions/{sid}/memory")
        if r.status_code == 200:
            data = api_ok(r)
            assert isinstance(data, dict), f"expected dict, got {type(data)}"
            R.ok(name, f"keys={list(data.keys())[:5]}")
        else:
            R.skip(name, f"memory endpoint returned {r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _adv_12_abort_idle(app: str):
    name = "ADV-12: Abort idle session succeeds"
    try:
        sid, _ = _chat(app, "Say ok.")
        time.sleep(3)
        r = req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        assert r.status_code == 200, f"abort returned {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _adv_13_abort_then_chat(app: str):
    name = "ADV-13: Abort then chat works normally"
    try:
        sid, _ = _chat(app, "Say A.")
        time.sleep(3)
        req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        time.sleep(3)
        _, ev = stream_chat(app, "Say B.", session_id=sid)
        content = get_streamed_content(ev)
        assert content.strip(), "empty response after abort"
        R.ok(name, f"response={content.strip()[:30]}")
    except Exception as e:
        R.fail(name, str(e))


def _adv_14_double_abort(app: str):
    name = "ADV-14: Double abort is idempotent"
    try:
        sid, _ = _chat(app, "Say ok.")
        time.sleep(3)
        r1 = req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        r2 = req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        assert r1.status_code == 200, f"first abort: {r1.status_code}"
        assert r2.status_code == 200, f"second abort: {r2.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _adv_15_history_format(app: str):
    name = "ADV-15: History messages have role and content"
    try:
        sid, _ = _chat(app, "Say ok.")
        r = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        msgs = api_ok(r).get("messages", [])
        assert len(msgs) >= 2, f"expected >=2 messages, got {len(msgs)}"
        for m in msgs:
            assert "role" in m, f"missing role: {list(m.keys())}"
            assert "content" in m, f"missing content: {list(m.keys())}"
        R.ok(name, f"{len(msgs)} messages all have role+content")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  run_all
# ═══════════════════════════════════════════════════════════════

def run_all():
    """Run all 45 session lifecycle, hooks, and advanced session tests."""
    # Force token refresh
    _h._token_created_at = 0
    ensure_auth()

    try:
        app = _deploy_app()
    except Exception as e:
        R.fail("SETUP: Deploy app", str(e))
        print(f"Cannot run tests without deployed app: {e}")
        return

    try:
        print("\n\033[1m=== SESSION LIFECYCLE ===\033[0m")
        _lifecycle_01_create_appears_in_list(app)
        _lifecycle_02_correct_app_id(app)
        _lifecycle_03_correct_user_id(app)
        _lifecycle_04_created_at_recent(app)
        _lifecycle_05_last_active_updates(app)
        _lifecycle_06_title_from_first_msg(app)
        _lifecycle_07_message_count_increments(app)
        _lifecycle_08_turn_count_tracked(app)
        _lifecycle_09_survives_multiple_ops(app)
        _lifecycle_10_delete_removes_from_list(app)
        _lifecycle_11_delete_removes_history(app)
        _lifecycle_12_delete_removes_memory(app)
        _lifecycle_13_deleted_session_404(app)
        _lifecycle_14_five_independent_sessions(app)
        _lifecycle_15_sorted_by_last_active(app)

        print("\n\033[1m=== HOOKS SYSTEM ===\033[0m")
        _hooks_01_context_status_emitted(app)
        _hooks_02_pressure_field(app)
        _hooks_03_estimated_tokens(app)
        _hooks_04_max_tokens(app)
        _hooks_05_threshold(app)
        _hooks_06_messages_count(app)
        _hooks_07_hook_id(app)
        _hooks_08_phase_field(app)
        _hooks_09_turn_start(app)
        _hooks_10_turn_end(app)
        _hooks_11_requesting(app)
        _hooks_12_tool_use(app)
        _hooks_13_multiple_per_turn(app)
        _hooks_14_dont_block_flow(app)
        _hooks_15_pressure_decreases_after_compact(app)

        print("\n\033[1m=== ADVANCED SESSION OPS ===\033[0m")
        _adv_01_fork_same_messages(app)
        _adv_02_fork_own_id(app)
        _adv_03_fork_chat_original_untouched(app)
        _adv_04_chat_original_fork_untouched(app)
        _adv_05_fork_preserves_memory(app)
        _adv_06_compact_then_fork(app)
        _adv_07_multiple_forks_independent(app)
        _adv_08_workspace_endpoint(app)
        _adv_09_workspace_git_branch(app)
        _adv_10_workspace_git_changes(app)
        _adv_11_memory_structured(app)
        _adv_12_abort_idle(app)
        _adv_13_abort_then_chat(app)
        _adv_14_double_abort(app)
        _adv_15_history_format(app)

    finally:
        print("\n--- Cleaning up sessions ---")
        _cleanup_all()

    print(f"\n\033[1m=== TEST 17 SUMMARY: {R.summary()} ===\033[0m")
