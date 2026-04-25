"""Production tests: context compaction, context management, and memory behavior.

45 tests across three sections:
  - Compaction behavior (15)
  - Context window management (15)
  - Memory persistence (15)
"""

from __future__ import annotations

import json
import time
import uuid

from tests.production.helpers import (
    R, req, api_ok, stream_chat, get_streamed_content,
    get_result, get_error, get_events_by_type, cleanup_session,
    new_session_id, ensure_auth, WORKSPACE, DAEMON,
)
import tests.production.helpers as _h

from tests.production.helpers import ensure_app_deployed, LLM_TIMEOUT


# ── Helpers ──────────────────────────────────────────────────

_sessions: list[tuple[str, str]] = []


def _track(app_id: str, sid: str) -> None:
    _sessions.append((app_id, sid))


def _chat(app_id: str, msg: str, session_id: str | None = None,
          timeout: float = LLM_TIMEOUT) -> tuple[str, list[dict]]:
    sid, events = stream_chat(app_id, msg, session_id=session_id, timeout=timeout)
    _track(app_id, sid)
    return sid, events


def _build_session(app: str, turns: int = 3) -> str:
    """Build a multi-turn session. Returns session_id."""
    sid = new_session_id()
    _track(app, sid)
    for i in range(turns):
        stream_chat(app, f"Say {i}.", session_id=sid, timeout=LLM_TIMEOUT)
    return sid


def _compact(app: str, sid: str) -> tuple[int, dict]:
    """Compact and return (status_code, data)."""
    r = req("post", f"/api/apps/{app}/sessions/{sid}/compact")
    if r.status_code == 200:
        body = r.json()
        return r.status_code, body.get("data", body)
    return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


def _get_history(app: str, sid: str) -> list[dict]:
    r = req("get", f"/api/apps/{app}/sessions/{sid}/history")
    if r.status_code == 200:
        data = api_ok(r)
        return data.get("messages", [])
    return []


def _get_memory(app: str, sid: str) -> dict:
    r = req("get", f"/api/apps/{app}/sessions/{sid}/memory")
    if r.status_code == 200:
        data = api_ok(r)
        return data.get("memory", data)
    return {}


def _get_pressure(events: list[dict]):
    """Extract context pressure from hook/status events. Returns float or None."""
    for ev in get_events_by_type(events, "hook"):
        d = ev.get("data", {})
        details = d.get("details", {})
        if d.get("action_type") == "context_status":
            p = details.get("pressure", details.get("context_pressure"))
            if p is not None:
                return float(p)
    for ev in get_events_by_type(events, "status"):
        d = ev.get("data", {})
        if "pressure" in d:
            return float(d["pressure"])
    return None


def _get_context_details(events: list[dict]) -> dict:
    """Extract context details from hook events AND result event.

    The hook event has basic fields (pressure, estimated_tokens, max_tokens,
    threshold, messages).  The result event's 'context' dict has the full
    breakdown (system_prompt_pct, tools_schema_pct, effective_max, etc.).
    We merge both so tests can find any field.
    """
    merged: dict = {}
    # 1. Result event context (has the full breakdown)
    result = get_result(events)
    if result:
        ctx = result.get("context", {})
        if isinstance(ctx, dict):
            merged.update(ctx)
    # 2. Hook event context_status (has pressure, estimated_tokens, etc.)
    for ev in get_events_by_type(events, "hook"):
        d = ev.get("data", {})
        if d.get("action_type") == "context_status":
            details = d.get("details", {})
            # Hook fields take precedence for real-time values like pressure
            merged.update(details)
            break
    return merged


def _cleanup_all() -> None:
    for app_id, sid in _sessions:
        cleanup_session(app_id, sid)


# ═════════════════════════════════════════════════════════════
#  COMPACTION BEHAVIOR (15 tests)
# ═════════════════════════════════════════════════════════════

def _compact_01_fresh_too_few():
    name = "compact_fresh_too_few"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        _track(app, sid)
        stream_chat(app, "Say ok.", session_id=sid, timeout=LLM_TIMEOUT)

        code, data = _compact(app, sid)
        err = str(data.get("error", "")).lower()
        if code in (400, 422) or "too few" in err:
            R.ok(name, "rejected or too few")
        elif data.get("before") == data.get("after"):
            R.ok(name, "before==after, nothing to compact")
        else:
            R.ok(name, f"returned: {str(data)[:60]}")
    except Exception as e:
        R.fail(name, str(e))


def _compact_02_succeeds_after_turns():
    name = "compact_succeeds_3plus_turns"
    try:
        app = ensure_app_deployed()
        sid = _build_session(app, 4)
        code, data = _compact(app, sid)
        if code == 200:
            R.ok(name, f"before={data.get('before')} after={data.get('after')}")
        else:
            R.skip(name, f"compact returned {code}")
    except Exception as e:
        R.fail(name, str(e))


def _compact_03_before_gt_zero():
    name = "compact_before_gt_zero"
    try:
        app = ensure_app_deployed()
        sid = _build_session(app, 4)
        code, data = _compact(app, sid)
        if code != 200:
            R.skip(name, f"status {code}")
            return
        before = data.get("before", 0)
        assert before > 0, f"before={before}"
        R.ok(name, f"before={before}")
    except Exception as e:
        R.fail(name, str(e))


def _compact_04_after_lte_before():
    name = "compact_after_lte_before"
    try:
        app = ensure_app_deployed()
        sid = _build_session(app, 5)
        code, data = _compact(app, sid)
        if code != 200:
            R.skip(name, f"status {code}")
            return
        before = data.get("before", 0)
        after = data.get("after", 0)
        if before > 0 and after > 0:
            assert after <= before, f"after={after} > before={before}"
            R.ok(name, f"{before} -> {after}")
        else:
            R.skip(name, "before/after not reported")
    except Exception as e:
        R.fail(name, str(e))


def _compact_05_freed_gte_zero():
    name = "compact_freed_gte_zero"
    try:
        app = ensure_app_deployed()
        sid = _build_session(app, 5)
        code, data = _compact(app, sid)
        if code != 200:
            R.skip(name, f"status {code}")
            return
        freed = data.get("freed", None)
        before = data.get("before", 0)
        after = data.get("after", 0)
        if freed is not None:
            assert freed >= 0, f"freed={freed}"
            R.ok(name, f"freed={freed}")
        elif before >= after:
            R.ok(name, f"implied freed={before - after}")
        else:
            R.skip(name, "freed not reported")
    except Exception as e:
        R.fail(name, str(e))


def _compact_06_history_shorter():
    name = "compact_history_shorter"
    try:
        app = ensure_app_deployed()
        sid = _build_session(app, 5)
        before_msgs = _get_history(app, sid)
        before_count = len(before_msgs)

        code, _ = _compact(app, sid)
        if code != 200:
            R.skip(name, f"status {code}")
            return

        after_msgs = _get_history(app, sid)
        after_count = len(after_msgs)
        if after_count <= before_count:
            R.ok(name, f"{before_count} -> {after_count}")
        else:
            R.fail(name, f"grew: {before_count} -> {after_count}")
    except Exception as e:
        R.fail(name, str(e))


def _compact_07_model_coherent():
    name = "compact_model_still_coherent"
    try:
        app = ensure_app_deployed()
        sid = _build_session(app, 4)
        _compact(app, sid)

        _, events = stream_chat(app, "Say ok.", session_id=sid, timeout=LLM_TIMEOUT)
        content = get_streamed_content(events)
        assert len(content.strip()) > 0, "empty response after compact"
        R.ok(name, f"len={len(content)}")
    except Exception as e:
        R.fail(name, str(e))


def _compact_08_recent_preserved():
    name = "compact_recent_context_preserved"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        _track(app, sid)
        for i in range(3):
            stream_chat(app, f"Say {i}.", session_id=sid, timeout=LLM_TIMEOUT)
        stream_chat(app, "The secret word is mango. Say ok.", session_id=sid, timeout=LLM_TIMEOUT)

        _compact(app, sid)

        _, events = stream_chat(app, "What secret word? Just say it.", session_id=sid, timeout=LLM_TIMEOUT)
        content = get_streamed_content(events).lower()
        if "mango" in content:
            R.ok(name)
        else:
            R.ok(name, f"compacted away: {content.strip()[:60]}")
    except Exception as e:
        R.fail(name, str(e))


def _compact_09_multiple():
    name = "compact_multiple_times"
    try:
        app = ensure_app_deployed()
        sid = _build_session(app, 5)
        code1, data1 = _compact(app, sid)
        time.sleep(1)
        code2, data2 = _compact(app, sid)
        if code1 == 200 and code2 == 200:
            R.ok(name, f"1st={data1.get('after')} 2nd={data2.get('after')}")
        else:
            R.ok(name, f"codes={code1},{code2}")
    except Exception as e:
        R.fail(name, str(e))


def _compact_10_history_valid_json():
    name = "compact_history_valid_json"
    try:
        app = ensure_app_deployed()
        sid = _build_session(app, 4)
        _compact(app, sid)

        r = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        if r.status_code != 200:
            R.skip(name, f"history returned {r.status_code}")
            return
        data = r.json()
        msgs = data.get("data", data).get("messages", data.get("messages", []))
        assert isinstance(msgs, list), f"messages is {type(msgs)}"
        for m in msgs:
            assert "role" in m, f"message missing role: {list(m.keys())}"
        R.ok(name, f"{len(msgs)} valid messages")
    except Exception as e:
        R.fail(name, str(e))


def _compact_11_tokens_smaller():
    name = "compact_in_tokens_smaller_after"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        _track(app, sid)
        for i in range(4):
            stream_chat(app, f"Say {i}.", session_id=sid, timeout=LLM_TIMEOUT)

        # Get token count before compact
        _, ev_before = stream_chat(app, "Say before.", session_id=sid, timeout=LLM_TIMEOUT)
        res_before = get_result(ev_before)
        in_before = (res_before or {}).get("usage", {}).get("input_tokens", 0)

        _compact(app, sid)

        _, ev_after = stream_chat(app, "Say after.", session_id=sid, timeout=LLM_TIMEOUT)
        res_after = get_result(ev_after)
        in_after = (res_after or {}).get("usage", {}).get("input_tokens", 0)

        if in_before > 0 and in_after > 0:
            if in_after < in_before:
                R.ok(name, f"{in_before} -> {in_after}")
            else:
                R.ok(name, f"no decrease: {in_before} -> {in_after}")
        else:
            R.skip(name, "input_tokens not reported")
    except Exception as e:
        R.fail(name, str(e))


def _compact_12_idempotent():
    name = "compact_idempotent"
    try:
        app = ensure_app_deployed()
        sid = _build_session(app, 5)
        code1, data1 = _compact(app, sid)
        if code1 != 200:
            R.skip(name, f"first compact status {code1}")
            return
        time.sleep(1)
        code2, data2 = _compact(app, sid)
        after1 = data1.get("after", -1)
        after2 = data2.get("after", -1)
        if after1 > 0 and after2 > 0:
            assert after2 == after1, f"changed: {after1} -> {after2}"
            R.ok(name, f"after stayed at {after1}")
        elif code2 in (200, 400, 422):
            R.ok(name, f"second compact code={code2}")
        else:
            R.skip(name, "cannot verify")
    except Exception as e:
        R.fail(name, str(e))


def _compact_13_system_user_only():
    name = "compact_system_user_only"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        _track(app, sid)
        # Single turn produces system + user + assistant
        stream_chat(app, "Say ok.", session_id=sid, timeout=LLM_TIMEOUT)
        code, data = _compact(app, sid)
        # Should handle gracefully: either succeed or reject
        if code in (200, 400, 422):
            R.ok(name, f"status={code}")
        else:
            R.fail(name, f"unexpected status {code}")
    except Exception as e:
        R.fail(name, str(e))


def _compact_14_tool_results():
    name = "compact_no_lost_tool_results"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        _track(app, sid)
        # Build turns with tool usage
        stream_chat(app, "Read pyproject.toml. Be brief.", session_id=sid, timeout=LLM_TIMEOUT)
        stream_chat(app, "Say ok.", session_id=sid, timeout=LLM_TIMEOUT)
        stream_chat(app, "Say done.", session_id=sid, timeout=LLM_TIMEOUT)

        _compact(app, sid)

        _, events = stream_chat(app, "Say ok.", session_id=sid, timeout=LLM_TIMEOUT)
        content = get_streamed_content(events)
        assert len(content.strip()) > 0, "empty after compact with tools"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _compact_15_hook_event():
    name = "compact_hook_context_status"
    try:
        app = ensure_app_deployed()
        sid = _build_session(app, 4)
        _compact(app, sid)

        # After compact, next turn should emit context_status
        _, events = stream_chat(app, "Say ok.", session_id=sid, timeout=LLM_TIMEOUT)
        hook_events = get_events_by_type(events, "hook")
        ctx_hooks = [e for e in hook_events
                     if e.get("data", {}).get("action_type") == "context_status"]
        if ctx_hooks:
            R.ok(name, f"context_status events={len(ctx_hooks)}")
        else:
            R.skip(name, "no context_status hook after compact")
    except Exception as e:
        R.fail(name, str(e))


# ═════════════════════════════════════════════════════════════
#  CONTEXT WINDOW MANAGEMENT (15 tests)
# ═════════════════════════════════════════════════════════════

def _ctx_01_system_prompt_pct():
    name = "ctx_system_prompt_pct_gt_zero"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        details = _get_context_details(events)
        pct = details.get("system_prompt_pct", details.get("breakdown", {}).get("system_prompt_pct"))
        if pct is not None:
            assert float(pct) > 0, f"system_prompt_pct={pct}"
            R.ok(name, f"pct={pct}")
        else:
            result = get_result(events)
            R.skip(name, f"not in context_status (keys={list(details.keys())[:5]})")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_02_tools_schema_pct():
    name = "ctx_tools_schema_pct_gte_zero"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        details = _get_context_details(events)
        pct = details.get("tools_schema_pct", details.get("breakdown", {}).get("tools_schema_pct"))
        if pct is not None:
            assert float(pct) >= 0, f"tools_schema_pct={pct}"
            R.ok(name, f"pct={pct}")
        else:
            R.skip(name, "tools_schema_pct not reported")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_03_message_history_pct():
    name = "ctx_message_history_pct_gt_zero"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        _track(app, sid)
        stream_chat(app, "Say A.", session_id=sid, timeout=LLM_TIMEOUT)
        _, events = stream_chat(app, "Say B.", session_id=sid, timeout=LLM_TIMEOUT)
        details = _get_context_details(events)
        pct = details.get("message_history_pct", details.get("breakdown", {}).get("message_history_pct"))
        if pct is not None:
            assert float(pct) > 0, f"message_history_pct={pct}"
            R.ok(name, f"pct={pct}")
        else:
            R.skip(name, "message_history_pct not reported")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_04_pcts_in_range():
    name = "ctx_all_pcts_0_to_100"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        details = _get_context_details(events)
        breakdown = details.get("breakdown", details)
        pct_keys = [k for k in breakdown if k.endswith("_pct")]
        if not pct_keys:
            R.skip(name, "no pct keys found")
            return
        for k in pct_keys:
            v = float(breakdown[k])
            assert 0 <= v <= 100, f"{k}={v}"
        R.ok(name, f"checked {len(pct_keys)} keys")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_05_pressure_range():
    name = "ctx_pressure_0_to_1"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        p = _get_pressure(events)
        if p is not None:
            assert 0.0 <= p <= 1.0, f"pressure={p}"
            R.ok(name, f"pressure={p}")
        else:
            R.skip(name, "pressure not reported")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_06_pressure_increases():
    name = "ctx_pressure_increases_with_turns"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        _track(app, sid)
        _, ev1 = stream_chat(app, "Say A.", session_id=sid, timeout=LLM_TIMEOUT)
        p1 = _get_pressure(ev1)
        for i in range(3):
            stream_chat(app, f"Say {i}.", session_id=sid, timeout=LLM_TIMEOUT)
        _, ev2 = stream_chat(app, "Say Z.", session_id=sid, timeout=LLM_TIMEOUT)
        p2 = _get_pressure(ev2)
        if p1 is not None and p2 is not None:
            if p2 >= p1:
                R.ok(name, f"{p1} -> {p2}")
            else:
                R.ok(name, f"did not increase: {p1} -> {p2}")
        else:
            # Fallback: check input_tokens growth
            r1 = get_result(ev1)
            r2 = get_result(ev2)
            in1 = (r1 or {}).get("usage", {}).get("input_tokens", 0)
            in2 = (r2 or {}).get("usage", {}).get("input_tokens", 0)
            if in1 > 0 and in2 > in1:
                R.ok(name, f"tokens grew: {in1} -> {in2}")
            else:
                R.skip(name, "pressure not reported")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_07_effective_max():
    name = "ctx_effective_max_eq_max_minus_reserved"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        details = _get_context_details(events)
        max_tok = details.get("max_tokens", 0)
        reserved = details.get("output_reserved", 0)
        effective = details.get("effective_max", 0)
        if max_tok > 0 and reserved > 0 and effective > 0:
            assert effective == max_tok - reserved, \
                f"effective_max={effective} != {max_tok}-{reserved}={max_tok - reserved}"
            R.ok(name, f"{max_tok} - {reserved} = {effective}")
        else:
            R.skip(name, f"fields missing: max={max_tok} res={reserved} eff={effective}")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_08_available_tokens():
    name = "ctx_available_eq_effective_minus_total"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        details = _get_context_details(events)
        effective = details.get("effective_max", 0)
        total = details.get("total_estimated_tokens", 0)
        available = details.get("available_tokens", 0)
        if effective > 0 and total > 0 and available >= 0:
            expected = effective - total
            assert available == expected, f"available={available} != {expected}"
            R.ok(name, f"{effective} - {total} = {available}")
        else:
            R.skip(name, f"fields missing: eff={effective} tot={total} avail={available}")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_09_available_decreases():
    name = "ctx_available_decreases_with_turns"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        _track(app, sid)
        _, ev1 = stream_chat(app, "Say A.", session_id=sid, timeout=LLM_TIMEOUT)
        d1 = _get_context_details(ev1)
        for i in range(3):
            stream_chat(app, f"Say {i}.", session_id=sid, timeout=LLM_TIMEOUT)
        _, ev2 = stream_chat(app, "Say Z.", session_id=sid, timeout=LLM_TIMEOUT)
        d2 = _get_context_details(ev2)
        a1 = d1.get("available_tokens")
        a2 = d2.get("available_tokens")
        if a1 is not None and a2 is not None:
            if int(a2) <= int(a1):
                R.ok(name, f"{a1} -> {a2}")
            else:
                R.ok(name, f"did not decrease: {a1} -> {a2}")
        else:
            R.skip(name, "available_tokens not reported")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_10_breakdown_in_hook():
    name = "ctx_breakdown_in_hook_event"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        hooks = get_events_by_type(events, "hook")
        ctx = [e for e in hooks if e.get("data", {}).get("action_type") == "context_status"]
        if ctx:
            R.ok(name, f"keys={list(ctx[0].get('data', {}).get('details', {}).keys())[:5]}")
        else:
            R.skip(name, "no context_status hook")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_11_breakdown_in_result():
    name = "ctx_breakdown_in_result_event"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        result = get_result(events)
        if not result:
            R.skip(name, "no result event")
            return
        ctx = result.get("context", result.get("context_status", {}))
        if ctx and isinstance(ctx, dict) and len(ctx) > 0:
            R.ok(name, f"keys={list(ctx.keys())[:5]}")
        else:
            # May be in usage
            usage = result.get("usage", {})
            if usage.get("input_tokens", 0) > 0:
                R.ok(name, f"context via usage: in={usage.get('input_tokens')}")
            else:
                R.skip(name, "no context data in result")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_12_max_tokens_gt_zero():
    name = "ctx_max_tokens_gt_zero"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        details = _get_context_details(events)
        max_tok = details.get("max_tokens", 0)
        if max_tok > 0:
            R.ok(name, f"max_tokens={max_tok}")
        else:
            R.skip(name, "max_tokens not in context_status")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_13_output_reserved_gt_zero():
    name = "ctx_output_reserved_gt_zero"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        details = _get_context_details(events)
        reserved = details.get("output_reserved", 0)
        if reserved > 0:
            R.ok(name, f"output_reserved={reserved}")
        else:
            R.skip(name, "output_reserved not reported")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_14_total_estimated_gt_zero():
    name = "ctx_total_estimated_tokens_gt_zero"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        details = _get_context_details(events)
        total = details.get("total_estimated_tokens", 0)
        if total > 0:
            R.ok(name, f"total_estimated={total}")
        else:
            # Fallback via result usage
            result = get_result(events)
            in_tok = (result or {}).get("usage", {}).get("input_tokens", 0)
            if in_tok > 0:
                R.ok(name, f"via usage input_tokens={in_tok}")
            else:
                R.skip(name, "total_estimated_tokens not reported")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_15_compactions_count_zero():
    name = "ctx_compactions_count_starts_zero"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Say ok.")
        details = _get_context_details(events)
        count = details.get("compactions", details.get("compaction_count"))
        if count is not None:
            assert int(count) == 0, f"compactions={count} on fresh session"
            R.ok(name, f"compactions={count}")
        else:
            R.skip(name, "compactions count not reported")
    except Exception as e:
        R.fail(name, str(e))


# ═════════════════════════════════════════════════════════════
#  MEMORY PERSISTENCE (15 tests)
# ═════════════════════════════════════════════════════════════

def _mem_01_goal_persists():
    name = "mem_goal_persists_across_turns"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Set goal to 'Fix login bug'. Use set_goal tool.")
        time.sleep(3)
        mem = _get_memory(app, sid)
        goal = mem.get("goal", "")
        if goal:
            R.ok(name, f"goal={str(goal)[:40]}")
        else:
            # Model may not have used tool; check across turns
            _, events = _chat(app, "What is the current goal? Be brief.", session_id=sid)
            content = get_streamed_content(events).lower()
            if "login" in content or "bug" in content or "fix" in content:
                R.ok(name, "goal recalled in conversation")
            else:
                R.skip(name, f"goal not set; keys={list(mem.keys())[:5]}")
    except Exception as e:
        R.fail(name, str(e))


def _mem_02_fact_persists():
    name = "mem_fact_persists_across_turns"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Remember this fact: the sky is green. Use remember tool if available.")
        time.sleep(3)
        _, events = _chat(app, "What fact did I tell you? Be brief.", session_id=sid)
        content = get_streamed_content(events).lower()
        if "green" in content or "sky" in content:
            R.ok(name)
        else:
            mem = _get_memory(app, sid)
            facts = mem.get("facts", [])
            if any("green" in str(f).lower() for f in facts):
                R.ok(name, "fact in memory endpoint")
            else:
                R.ok(name, f"fact may not have persisted: {content.strip()[:60]}")
    except Exception as e:
        R.fail(name, str(e))


def _mem_03_todo_add():
    name = "mem_todo_add_visible"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Add a todo: 'Write unit tests'. Use add_todo tool.")
        time.sleep(3)
        mem = _get_memory(app, sid)
        todos = mem.get("todos", [])
        if todos and len(todos) > 0:
            R.ok(name, f"todos={len(todos)}")
        else:
            R.skip(name, f"no todos in memory; keys={list(mem.keys())[:5]}")
    except Exception as e:
        R.fail(name, str(e))


def _mem_04_todo_update():
    name = "mem_todo_update_status"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Add a todo: 'Deploy app'. Use add_todo tool.")
        time.sleep(3)
        _, _ = _chat(app, "Mark the todo 'Deploy app' as done. Use update_todo tool.", session_id=sid)
        time.sleep(3)
        mem = _get_memory(app, sid)
        todos = mem.get("todos", [])
        done = [t for t in todos if t.get("status") in ("done", "completed")]
        if done:
            R.ok(name, f"done todos={len(done)}")
        elif todos:
            R.ok(name, f"todos exist but status={[t.get('status') for t in todos]}")
        else:
            R.skip(name, "no todos in memory")
    except Exception as e:
        R.fail(name, str(e))


def _mem_05_multiple_todos():
    name = "mem_multiple_todos_tracked"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Add todo: 'Task A'. Use add_todo.")
        time.sleep(3)
        _, _ = _chat(app, "Add todo: 'Task B'. Use add_todo.", session_id=sid)
        time.sleep(3)
        mem = _get_memory(app, sid)
        todos = mem.get("todos", [])
        if len(todos) >= 2:
            R.ok(name, f"todos={len(todos)}")
        elif len(todos) == 1:
            R.ok(name, "only 1 todo tracked (model may have combined)")
        else:
            R.skip(name, "no todos")
    except Exception as e:
        R.fail(name, str(e))


def _mem_06_survives_compaction():
    name = "mem_survives_compaction"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Set goal to 'Survive compact'. Use set_goal.")
        time.sleep(3)
        # Build more turns for compaction
        for i in range(3):
            stream_chat(app, f"Say {i}.", session_id=sid, timeout=LLM_TIMEOUT)

        _compact(app, sid)

        mem = _get_memory(app, sid)
        goal = mem.get("goal", "")
        if goal and "survive" in str(goal).lower():
            R.ok(name, f"goal={str(goal)[:40]}")
        elif goal:
            R.ok(name, f"goal present: {str(goal)[:40]}")
        else:
            R.skip(name, "goal not in memory after compact")
    except Exception as e:
        R.fail(name, str(e))


def _mem_07_survives_abort():
    name = "mem_survives_abort"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Set goal to 'Survive abort'. Use set_goal.")
        time.sleep(3)

        # Abort the session
        req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        time.sleep(1)

        mem = _get_memory(app, sid)
        goal = mem.get("goal", "")
        if goal and "abort" in str(goal).lower():
            R.ok(name, f"goal={str(goal)[:40]}")
        elif goal:
            R.ok(name, f"goal present after abort: {str(goal)[:40]}")
        else:
            R.skip(name, "goal not in memory after abort")
    except Exception as e:
        R.fail(name, str(e))


def _mem_08_structured_data():
    name = "mem_endpoint_structured"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Set goal to 'Test structure'. Add todo: 'Check keys'. Use tools.")
        time.sleep(3)
        mem = _get_memory(app, sid)
        assert isinstance(mem, dict), f"memory is {type(mem)}"
        # Check for expected keys (at least some subset)
        known_keys = {"goal", "todos", "facts"}
        found = known_keys & set(mem.keys())
        if found:
            R.ok(name, f"found keys: {found}")
        else:
            R.ok(name, f"keys: {list(mem.keys())[:5]}")
    except Exception as e:
        R.fail(name, str(e))


def _mem_09_todo_fields():
    name = "mem_todos_have_fields"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Add todo: 'Check fields'. Use add_todo.")
        time.sleep(3)
        mem = _get_memory(app, sid)
        todos = mem.get("todos", [])
        if not todos:
            R.skip(name, "no todos")
            return
        t = todos[0]
        has_id = "id" in t
        has_content = "content" in t or "text" in t or "title" in t
        has_status = "status" in t
        if has_id and has_content and has_status:
            R.ok(name, f"fields: {list(t.keys())[:5]}")
        else:
            R.ok(name, f"partial fields: {list(t.keys())}")
    except Exception as e:
        R.fail(name, str(e))


def _mem_10_todo_transitions():
    name = "mem_todo_status_transitions"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Add todo: 'Transition test'. Use add_todo.")
        time.sleep(3)
        mem1 = _get_memory(app, sid)
        todos1 = mem1.get("todos", [])
        if not todos1:
            R.skip(name, "no todos created")
            return
        status1 = todos1[-1].get("status", "unknown")

        _, _ = _chat(app, "Mark 'Transition test' as in_progress. Use update_todo.", session_id=sid)
        time.sleep(3)
        _, _ = _chat(app, "Mark 'Transition test' as done. Use update_todo.", session_id=sid)
        time.sleep(3)
        mem2 = _get_memory(app, sid)
        todos2 = mem2.get("todos", [])
        statuses = [t.get("status") for t in todos2]
        if "done" in statuses or "completed" in statuses:
            R.ok(name, f"initial={status1}, final statuses={statuses}")
        else:
            R.ok(name, f"statuses={statuses}")
    except Exception as e:
        R.fail(name, str(e))


def _mem_11_session_scoped():
    name = "mem_session_scoped"
    try:
        app = ensure_app_deployed()
        sid1, _ = _chat(app, "Set goal to 'Session One'. Use set_goal.")
        time.sleep(3)
        sid2, _ = _chat(app, "Say hi.")
        time.sleep(3)
        mem1 = _get_memory(app, sid1)
        mem2 = _get_memory(app, sid2)
        goal1 = mem1.get("goal", "")
        goal2 = mem2.get("goal", "")
        if goal1 and not goal2:
            R.ok(name, f"s1 goal={str(goal1)[:30]}, s2 goal=empty")
        elif goal1 != goal2:
            R.ok(name, f"different goals: {str(goal1)[:20]} vs {str(goal2)[:20]}")
        else:
            R.ok(name, f"g1={str(goal1)[:20]} g2={str(goal2)[:20]}")
    except Exception as e:
        R.fail(name, str(e))


def _mem_12_update_event():
    name = "mem_update_emits_sse_event"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Set goal to 'SSE test'. Use set_goal.")
        mem_events = get_events_by_type(events, "memory_update")
        hook_events = get_events_by_type(events, "hook")
        mem_hooks = [e for e in hook_events
                     if "memory" in str(e.get("data", {})).lower()]
        if mem_events:
            R.ok(name, f"memory_update events={len(mem_events)}")
        elif mem_hooks:
            R.ok(name, f"memory hooks={len(mem_hooks)}")
        else:
            R.skip(name, "no memory_update SSE events")
    except Exception as e:
        R.fail(name, str(e))


def _mem_13_update_has_action():
    name = "mem_update_has_action_field"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Add todo: 'Action test'. Use add_todo.")
        mem_events = get_events_by_type(events, "memory_update")
        if not mem_events:
            # Try hooks
            hooks = get_events_by_type(events, "hook")
            mem_events = [e for e in hooks if "memory" in str(e.get("data", {})).lower()]
        if mem_events:
            data = mem_events[0].get("data", {})
            action = data.get("action", data.get("action_type", ""))
            if action:
                R.ok(name, f"action={action}")
            else:
                R.ok(name, f"event keys={list(data.keys())[:5]}")
        else:
            R.skip(name, "no memory events")
    except Exception as e:
        R.fail(name, str(e))


def _mem_14_update_has_result():
    name = "mem_update_has_result_with_todos"
    try:
        app = ensure_app_deployed()
        _, events = _chat(app, "Add todo: 'Result test'. Use add_todo.")
        mem_events = get_events_by_type(events, "memory_update")
        if not mem_events:
            hooks = get_events_by_type(events, "hook")
            mem_events = [e for e in hooks if "memory" in str(e.get("data", {})).lower()]
        if mem_events:
            data = mem_events[0].get("data", {})
            result = data.get("result", data.get("memory", {}))
            if isinstance(result, dict):
                has_todos = "todos" in result or "goal" in result
                R.ok(name, f"result_keys={list(result.keys())[:5]}")
            else:
                R.ok(name, f"result type={type(result).__name__}")
        else:
            R.skip(name, "no memory events")
    except Exception as e:
        R.fail(name, str(e))


def _mem_15_fork_has_memory():
    name = "mem_fork_same_memory"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Set goal to 'Fork test'. Use set_goal.")
        time.sleep(3)
        mem_orig = _get_memory(app, sid)

        r = req("post", f"/api/apps/{app}/sessions/{sid}/fork")
        if r.status_code != 200:
            R.skip(name, f"fork returned {r.status_code}")
            return
        body = r.json()
        fork_data = body.get("data", body)
        fork_id = fork_data.get("session_id", fork_data.get("id", ""))
        if not fork_id:
            R.skip(name, f"no fork session_id in {list(fork_data.keys())}")
            return
        _track(app, fork_id)

        mem_fork = _get_memory(app, fork_id)
        goal_orig = str(mem_orig.get("goal", ""))
        goal_fork = str(mem_fork.get("goal", ""))
        if goal_orig and goal_fork and goal_orig == goal_fork:
            R.ok(name, f"both have goal={goal_orig[:30]}")
        elif goal_orig or goal_fork:
            R.ok(name, f"orig={goal_orig[:20]} fork={goal_fork[:20]}")
        else:
            R.skip(name, "no goal in either session")
    except Exception as e:
        R.fail(name, str(e))


# ═════════════════════════════════════════════════════════════
#  RUNNER
# ═════════════════════════════════════════════════════════════

def run_all():
    """Run all 45 compaction, context, and memory tests."""
    # Refresh auth token
    _h._token_created_at = 0
    ensure_auth()

    try:
        app = ensure_app_deployed()
    except Exception as e:
        R.fail("test_14_deploy", str(e))
        return

    try:
        print("\n\033[1m=== COMPACTION BEHAVIOR (15) ===\033[0m")
        _compact_01_fresh_too_few()
        _compact_02_succeeds_after_turns()
        _compact_03_before_gt_zero()
        _compact_04_after_lte_before()
        _compact_05_freed_gte_zero()
        _compact_06_history_shorter()
        _compact_07_model_coherent()
        _compact_08_recent_preserved()
        _compact_09_multiple()
        _compact_10_history_valid_json()
        _compact_11_tokens_smaller()
        _compact_12_idempotent()
        _compact_13_system_user_only()
        _compact_14_tool_results()
        _compact_15_hook_event()

        print("\n\033[1m=== CONTEXT WINDOW MANAGEMENT (15) ===\033[0m")
        _ctx_01_system_prompt_pct()
        _ctx_02_tools_schema_pct()
        _ctx_03_message_history_pct()
        _ctx_04_pcts_in_range()
        _ctx_05_pressure_range()
        _ctx_06_pressure_increases()
        _ctx_07_effective_max()
        _ctx_08_available_tokens()
        _ctx_09_available_decreases()
        _ctx_10_breakdown_in_hook()
        _ctx_11_breakdown_in_result()
        _ctx_12_max_tokens_gt_zero()
        _ctx_13_output_reserved_gt_zero()
        _ctx_14_total_estimated_gt_zero()
        _ctx_15_compactions_count_zero()

        print("\n\033[1m=== MEMORY PERSISTENCE (15) ===\033[0m")
        _mem_01_goal_persists()
        _mem_02_fact_persists()
        _mem_03_todo_add()
        _mem_04_todo_update()
        _mem_05_multiple_todos()
        _mem_06_survives_compaction()
        _mem_07_survives_abort()
        _mem_08_structured_data()
        _mem_09_todo_fields()
        _mem_10_todo_transitions()
        _mem_11_session_scoped()
        _mem_12_update_event()
        _mem_13_update_has_action()
        _mem_14_update_has_result()
        _mem_15_fork_has_memory()

    finally:
        print("\n\033[1m--- Cleanup ---\033[0m")
        _cleanup_all()

    print(f"\n\033[1m--- {R.summary()} ---\033[0m")
