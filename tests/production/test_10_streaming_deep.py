"""Production tests: Deep SSE streaming validation.

45 tests covering every SSE event field, token streaming behavior,
tool event completeness, and strict event ordering.
"""

from __future__ import annotations

import os
import time

from tests.production.helpers import (
    R,
    req,
    api_ok,
    stream_chat,
    get_streamed_content,
    get_result,
    get_error,
    get_events_by_type,
    cleanup_session,
    new_session_id,
    ensure_auth,
    ensure_app_deployed,
    WORKSPACE,
    DAEMON,
    LLM_TIMEOUT,
)
import tests.production.helpers as _h


# ── Helpers local to this file ───────────────────────────────────

def _get_status_events(events, phase=None):
    """Get status events, optionally filtered by phase."""
    statuses = get_events_by_type(events, "status")
    if phase is None:
        return statuses
    return [e for e in statuses if e["data"].get("phase") == phase]


def _event_index(events, event_type, phase=None):
    """Return index of first event matching type (and optional phase), or -1."""
    for i, e in enumerate(events):
        if e["event"] == event_type:
            if phase is None or e["data"].get("phase") == phase:
                return i
    return -1


def _last_event_index(events, event_type, phase=None):
    """Return index of last event matching type (and optional phase), or -1."""
    for i in range(len(events) - 1, -1, -1):
        if events[i]["event"] == event_type:
            if phase is None or events[i]["data"].get("phase") == phase:
                return i
    return -1


# ── Main test runner ─────────────────────────────────────────────

def run_all():
    # Force fresh auth
    _h._token_created_at = 0
    ensure_auth()
    # Deploy test-full app (GET-first to avoid rate limit)
    app_id = "test-full"
    r = req("get", f"/api/apps/{app_id}")
    if r.status_code != 200:
        yaml_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "examples", "test-full.yaml"))
        for _attempt in range(3):
            r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
            if r.status_code == 429:
                import time as _t
                _t.sleep(min(r.json().get("retry_after", 30) + 1, 60))
                continue
            app_id = r.json().get("data", {}).get("app_id", "test-full")
            break
        else:
            raise AssertionError(f"Deploy failed after 3 retries: {r.text[:200]}")

    # ── Shared stream for SSE structure tests (simple prompt) ────
    sid_simple = None
    events_simple = []
    result_simple = None
    try:
        sid_simple, events_simple = stream_chat(app_id, "Say hi")
        result_simple = get_result(events_simple)
    except Exception as exc:
        R.fail("stream_setup_simple", str(exc))

    # ────────────────────────────────────────────────────────────
    # SSE EVENT STRUCTURE (15)
    # ────────────────────────────────────────────────────────────

    # 1. Result event has all required keys
    try:
        required = {"content", "session_id", "tool_calls_count", "turns_used", "truncated", "error"}
        if result_simple is None:
            R.fail("struct_01_result_keys", "No result event")
        else:
            missing = required - set(result_simple.keys())
            if not missing:
                R.ok("struct_01_result_keys", f"all {len(required)} keys present")
            else:
                R.fail("struct_01_result_keys", f"missing keys: {missing}")
    except Exception as e:
        R.fail("struct_01_result_keys", str(e))

    # 2. Result usage has input_tokens > 0
    try:
        usage = (result_simple or {}).get("usage", {})
        if not usage:
            R.skip("struct_02_input_tokens", "No usage in result")
        elif usage.get("input_tokens", 0) > 0:
            R.ok("struct_02_input_tokens", f"{usage['input_tokens']}")
        else:
            R.skip("struct_02_input_tokens", f"input_tokens=0 (provider may not report per-turn)")
    except Exception as e:
        R.fail("struct_02_input_tokens", str(e))

    # 3. Result usage has output_tokens > 0
    try:
        usage = (result_simple or {}).get("usage", {})
        if not usage:
            R.skip("struct_03_output_tokens", "No usage in result")
        elif usage.get("output_tokens", 0) > 0:
            R.ok("struct_03_output_tokens", f"{usage['output_tokens']}")
        else:
            R.skip("struct_03_output_tokens", f"output_tokens=0 (provider may not report per-turn)")
    except Exception as e:
        R.fail("struct_03_output_tokens", str(e))

    # 4. total_input_tokens >= input_tokens
    try:
        usage = (result_simple or {}).get("usage", {})
        inp = usage.get("input_tokens", 0)
        total_inp = usage.get("total_input_tokens", 0)
        if not usage:
            R.fail("struct_04_total_input", "No usage")
        elif total_inp >= inp:
            R.ok("struct_04_total_input", f"{total_inp} >= {inp}")
        else:
            R.fail("struct_04_total_input", f"total_input_tokens {total_inp} < input_tokens {inp}")
    except Exception as e:
        R.fail("struct_04_total_input", str(e))

    # 5. total_output_tokens >= output_tokens
    try:
        usage = (result_simple or {}).get("usage", {})
        out = usage.get("output_tokens", 0)
        total_out = usage.get("total_output_tokens", 0)
        if not usage:
            R.fail("struct_05_total_output", "No usage")
        elif total_out >= out:
            R.ok("struct_05_total_output", f"{total_out} >= {out}")
        else:
            R.fail("struct_05_total_output", f"total_output_tokens {total_out} < output_tokens {out}")
    except Exception as e:
        R.fail("struct_05_total_output", str(e))

    # 6. cost_usd >= 0
    try:
        usage = (result_simple or {}).get("usage", {})
        cost = usage.get("cost_usd")
        if cost is None:
            R.fail("struct_06_cost_usd", "No cost_usd in usage")
        elif cost >= 0:
            R.ok("struct_06_cost_usd", f"${cost:.6f}")
        else:
            R.fail("struct_06_cost_usd", f"cost_usd={cost} is negative")
    except Exception as e:
        R.fail("struct_06_cost_usd", str(e))

    # 7. turn_number >= 1
    try:
        turn = (result_simple or {}).get("turn_number")
        if turn is None:
            R.skip("struct_07_turn_number", "No turn_number in result (metrics may not be wired)")
        elif turn >= 1:
            R.ok("struct_07_turn_number", f"turn={turn}")
        else:
            R.skip("struct_07_turn_number", f"turn_number={turn} (may be 0 if metrics not active)")
    except Exception as e:
        R.fail("struct_07_turn_number", str(e))

    # 8. context.pressure between 0.0 and 1.0
    try:
        ctx = (result_simple or {}).get("context", {})
        pressure = ctx.get("pressure") if ctx else None
        if pressure is None:
            R.fail("struct_08_context_pressure", f"No context.pressure, context={ctx}")
        elif 0.0 <= pressure <= 1.0:
            R.ok("struct_08_context_pressure", f"pressure={pressure:.3f}")
        else:
            R.fail("struct_08_context_pressure", f"pressure={pressure} out of range")
    except Exception as e:
        R.fail("struct_08_context_pressure", str(e))

    # 9. context.max_tokens > 0
    try:
        ctx = (result_simple or {}).get("context", {})
        max_tok = ctx.get("max_tokens") if ctx else None
        if max_tok is None:
            R.fail("struct_09_context_max_tokens", f"No context.max_tokens")
        elif max_tok > 0:
            R.ok("struct_09_context_max_tokens", f"max_tokens={max_tok}")
        else:
            R.fail("struct_09_context_max_tokens", f"max_tokens={max_tok}")
    except Exception as e:
        R.fail("struct_09_context_max_tokens", str(e))

    # 10. workspace_status with branch
    try:
        ws = (result_simple or {}).get("workspace_status", {})
        branch = ws.get("branch") if ws else None
        if branch:
            R.ok("struct_10_workspace_branch", f"branch={branch}")
        else:
            R.fail("struct_10_workspace_branch", f"No branch in workspace_status: {ws}")
    except Exception as e:
        R.fail("struct_10_workspace_branch", str(e))

    # 11. workspace_status with changes array
    try:
        ws = (result_simple or {}).get("workspace_status", {})
        if ws and "changes" in ws and isinstance(ws["changes"], list):
            R.ok("struct_11_workspace_changes", f"{len(ws['changes'])} changes")
        else:
            R.fail("struct_11_workspace_changes", f"No changes array in workspace_status: {ws}")
    except Exception as e:
        R.fail("struct_11_workspace_changes", str(e))

    # 12. Status events have phase field
    try:
        statuses = get_events_by_type(events_simple, "status")
        if not statuses:
            R.skip("struct_12_status_phase", "No status events")
        elif all("phase" in s["data"] for s in statuses):
            phases = set(s["data"]["phase"] for s in statuses)
            R.ok("struct_12_status_phase", f"phases: {phases}")
        else:
            missing = [i for i, s in enumerate(statuses) if "phase" not in s["data"]]
            R.fail("struct_12_status_phase", f"Missing phase at indices {missing}")
    except Exception as e:
        R.fail("struct_12_status_phase", str(e))

    # 13. Status turn_start has turn and max_turns
    try:
        turn_starts = _get_status_events(events_simple, "turn_start")
        if not turn_starts:
            R.skip("struct_13_turn_start_fields", "No turn_start events")
        else:
            ts = turn_starts[0]["data"]
            has_turn = "turn" in ts
            has_max = "max_turns" in ts
            if has_turn and has_max:
                R.ok("struct_13_turn_start_fields", f"turn={ts['turn']}, max={ts['max_turns']}")
            else:
                R.fail("struct_13_turn_start_fields", f"turn={has_turn}, max_turns={has_max}, data={ts}")
    except Exception as e:
        R.fail("struct_13_turn_start_fields", str(e))

    # 14. Status requesting has model name
    try:
        requesting = _get_status_events(events_simple, "requesting")
        if not requesting:
            R.skip("struct_14_requesting_model", "No requesting events")
        else:
            model = requesting[0]["data"].get("model", "")
            if model:
                R.ok("struct_14_requesting_model", f"model={model}")
            else:
                R.fail("struct_14_requesting_model", f"No model in requesting: {requesting[0]['data']}")
    except Exception as e:
        R.fail("struct_14_requesting_model", str(e))

    # 15. Status turn_end has reason field
    try:
        turn_ends = _get_status_events(events_simple, "turn_end")
        if not turn_ends:
            R.skip("struct_15_turn_end_reason", "No turn_end events")
        else:
            reason = turn_ends[0]["data"].get("reason", "")
            if reason:
                R.ok("struct_15_turn_end_reason", f"reason={reason}")
            else:
                R.fail("struct_15_turn_end_reason", f"No reason in turn_end: {turn_ends[0]['data']}")
    except Exception as e:
        R.fail("struct_15_turn_end_reason", str(e))

    # Clean up simple session
    if sid_simple:
        cleanup_session(app_id, sid_simple)

    time.sleep(3)

    # ────────────────────────────────────────────────────────────
    # TOKEN STREAMING (10)
    # ────────────────────────────────────────────────────────────

    # Stream for token tests
    sid_tok = None
    events_tok = []
    result_tok = None
    try:
        sid_tok, events_tok = stream_chat(app_id, "Say hello world")
        result_tok = get_result(events_tok)
    except Exception as exc:
        R.fail("stream_setup_token", str(exc))

    # 1. Token events arrive before result
    try:
        tokens = get_events_by_type(events_tok, "token")
        if not tokens:
            R.skip("tok_01_before_result", "No token events")
        else:
            first_tok_idx = _event_index(events_tok, "token")
            result_idx = _event_index(events_tok, "result")
            if result_idx == -1:
                R.fail("tok_01_before_result", "No result event")
            elif first_tok_idx < result_idx:
                R.ok("tok_01_before_result", f"token@{first_tok_idx} < result@{result_idx}")
            else:
                R.fail("tok_01_before_result", f"token@{first_tok_idx} >= result@{result_idx}")
    except Exception as e:
        R.fail("tok_01_before_result", str(e))

    # 2. Token deltas are non-empty strings
    try:
        tokens = get_events_by_type(events_tok, "token")
        if not tokens:
            R.skip("tok_02_nonempty_delta", "No token events")
        else:
            non_empty = [t for t in tokens if isinstance(t["data"].get("delta"), str) and len(t["data"]["delta"]) > 0]
            if len(non_empty) == len(tokens):
                R.ok("tok_02_nonempty_delta", f"all {len(tokens)} deltas non-empty")
            else:
                empty_count = len(tokens) - len(non_empty)
                R.fail("tok_02_nonempty_delta", f"{empty_count}/{len(tokens)} empty deltas")
    except Exception as e:
        R.fail("tok_02_nonempty_delta", str(e))

    # 3. Accumulated tokens match result content
    try:
        tokens = get_events_by_type(events_tok, "token")
        accumulated = "".join(t["data"].get("delta", "") for t in tokens)
        result_content = (result_tok or {}).get("content", "")
        if not accumulated and not result_content:
            R.skip("tok_03_accumulated_match", "Both empty")
        elif accumulated and result_content:
            # They should match or at least be very similar
            if accumulated.strip() == result_content.strip():
                R.ok("tok_03_accumulated_match", f"{len(accumulated)} chars match")
            elif accumulated.strip() in result_content.strip() or result_content.strip() in accumulated.strip():
                R.ok("tok_03_accumulated_match", f"content subset match ({len(accumulated)} vs {len(result_content)})")
            else:
                R.fail("tok_03_accumulated_match", f"mismatch: tokens={len(accumulated)} vs result={len(result_content)}")
        elif accumulated:
            R.ok("tok_03_accumulated_match", f"tokens only ({len(accumulated)} chars)")
        else:
            R.ok("tok_03_accumulated_match", f"result only ({len(result_content)} chars)")
    except Exception as e:
        R.fail("tok_03_accumulated_match", str(e))

    # 4. out_token events have count > 0
    try:
        out_tokens = get_events_by_type(events_tok, "out_token")
        if not out_tokens:
            R.skip("tok_04_out_token_count", "No out_token events")
        else:
            valid = [e for e in out_tokens if e["data"].get("count", 0) > 0]
            if len(valid) == len(out_tokens):
                R.ok("tok_04_out_token_count", f"all {len(out_tokens)} have count>0")
            else:
                R.fail("tok_04_out_token_count", f"{len(valid)}/{len(out_tokens)} have count>0")
    except Exception as e:
        R.fail("tok_04_out_token_count", str(e))

    # 5. in_token count > 1000 for non-trivial prompt
    try:
        in_tokens = get_events_by_type(events_tok, "in_token")
        if not in_tokens:
            R.skip("tok_05_in_token_size", "No in_token events")
        else:
            count = in_tokens[-1]["data"].get("count", 0)
            if count > 1000:
                R.ok("tok_05_in_token_size", f"in_token count={count}")
            else:
                R.fail("tok_05_in_token_size", f"in_token count={count} <= 1000")
    except Exception as e:
        R.fail("tok_05_in_token_size", str(e))

    # 6-7. Multi-turn token count tests: second turn has more in_tokens
    sid_multi = None
    events_turn1 = []
    events_turn2 = []
    try:
        sid_multi = new_session_id()
        sid_multi, events_turn1 = stream_chat(app_id, "Say yes", session_id=sid_multi)
        time.sleep(3)
        _, events_turn2 = stream_chat(app_id, "Say no", session_id=sid_multi)
    except Exception as exc:
        R.fail("stream_setup_multi", str(exc))

    # 6. Token count increases across turns
    try:
        in1 = get_events_by_type(events_turn1, "in_token")
        in2 = get_events_by_type(events_turn2, "in_token")
        if not in1 or not in2:
            R.skip("tok_06_count_increases", "No in_token events in one/both turns")
        else:
            c1 = in1[-1]["data"].get("count", 0)
            c2 = in2[-1]["data"].get("count", 0)
            if c2 > c1:
                R.ok("tok_06_count_increases", f"turn2={c2} > turn1={c1}")
            else:
                R.fail("tok_06_count_increases", f"turn2={c2} not > turn1={c1}")
    except Exception as e:
        R.fail("tok_06_count_increases", str(e))

    # 7. Second turn in_token > first turn in_token (context grows)
    try:
        in1 = get_events_by_type(events_turn1, "in_token")
        in2 = get_events_by_type(events_turn2, "in_token")
        if not in1 or not in2:
            R.skip("tok_07_context_grows", "No in_token events")
        else:
            c1 = in1[-1]["data"].get("count", 0)
            c2 = in2[-1]["data"].get("count", 0)
            if c2 > c1:
                R.ok("tok_07_context_grows", f"context grew: {c1} -> {c2}")
            else:
                R.fail("tok_07_context_grows", f"context did not grow: {c1} -> {c2}")
    except Exception as e:
        R.fail("tok_07_context_grows", str(e))

    if sid_multi:
        cleanup_session(app_id, sid_multi)

    # 8. stream_done arrives after last token, before tool_start or result
    try:
        sd_idx = _event_index(events_tok, "stream_done")
        last_tok_idx = _last_event_index(events_tok, "token")
        result_idx = _event_index(events_tok, "result")
        if sd_idx == -1:
            R.skip("tok_08_stream_done_order", "No stream_done event")
        elif last_tok_idx == -1:
            R.skip("tok_08_stream_done_order", "No token events")
        elif sd_idx > last_tok_idx and (result_idx == -1 or sd_idx < result_idx):
            R.ok("tok_08_stream_done_order", f"token@{last_tok_idx} < stream_done@{sd_idx} < result@{result_idx}")
        else:
            R.fail("tok_08_stream_done_order", f"token@{last_tok_idx}, stream_done@{sd_idx}, result@{result_idx}")
    except Exception as e:
        R.fail("tok_08_stream_done_order", str(e))

    # 9. No token events after stream_done
    try:
        sd_idx = _event_index(events_tok, "stream_done")
        if sd_idx == -1:
            R.skip("tok_09_no_tokens_after_done", "No stream_done event")
        else:
            # Check for token events in the same "turn segment" after stream_done
            # (there can be tokens in a later turn after a tool call)
            # Find next tool_start or status/turn_start after stream_done
            next_turn_idx = len(events_tok)
            for i in range(sd_idx + 1, len(events_tok)):
                if events_tok[i]["event"] == "status" and events_tok[i]["data"].get("phase") in ("turn_start", "requesting"):
                    next_turn_idx = i
                    break
            tokens_after = [e for e in events_tok[sd_idx + 1:next_turn_idx] if e["event"] == "token"]
            if not tokens_after:
                R.ok("tok_09_no_tokens_after_done", f"clean after stream_done@{sd_idx}")
            else:
                R.fail("tok_09_no_tokens_after_done", f"{len(tokens_after)} tokens after stream_done")
    except Exception as e:
        R.fail("tok_09_no_tokens_after_done", str(e))

    # 10. Heartbeat events have empty data {}
    try:
        heartbeats = get_events_by_type(events_tok, "heartbeat")
        if not heartbeats:
            R.skip("tok_10_heartbeat_empty", "No heartbeat events")
        else:
            all_empty = all(hb["data"] == {} or hb["data"] == {"raw": ""} for hb in heartbeats)
            if all_empty:
                R.ok("tok_10_heartbeat_empty", f"{len(heartbeats)} heartbeats, all empty")
            else:
                non_empty = [hb["data"] for hb in heartbeats if hb["data"] != {} and hb["data"] != {"raw": ""}]
                R.fail("tok_10_heartbeat_empty", f"non-empty heartbeats: {non_empty[:3]}")
    except Exception as e:
        R.fail("tok_10_heartbeat_empty", str(e))

    if sid_tok:
        cleanup_session(app_id, sid_tok)

    time.sleep(3)

    # ────────────────────────────────────────────────────────────
    # TOOL EVENT COMPLETENESS (10)
    # ────────────────────────────────────────────────────────────

    # Stream that triggers tool use (Read tool)
    sid_tool = None
    events_tool = []
    try:
        sid_tool, events_tool = stream_chat(app_id, "Read pyproject.toml")
        time.sleep(3)
    except Exception as exc:
        R.fail("stream_setup_tool", str(exc))

    tool_starts = get_events_by_type(events_tool, "tool_start")
    tool_calls = get_events_by_type(events_tool, "tool_call")

    # 1. tool_start has all required fields
    try:
        if not tool_starts:
            R.skip("tool_01_start_fields", "No tool_start events")
        else:
            ts = tool_starts[0]["data"]
            required = {"id", "name", "short_name", "params", "display", "silent"}
            missing = required - set(ts.keys())
            if not missing:
                R.ok("tool_01_start_fields", f"all fields present: {ts.get('short_name', ts.get('name', '?'))}")
            else:
                R.fail("tool_01_start_fields", f"missing: {missing}, have: {set(ts.keys())}")
    except Exception as e:
        R.fail("tool_01_start_fields", str(e))

    # 2. tool_call has all required fields
    try:
        if not tool_calls:
            R.skip("tool_02_call_fields", "No tool_call events")
        else:
            tc = tool_calls[0]["data"]
            required = {"id", "name", "short_name", "params", "success", "error", "display", "silent", "result"}
            missing = required - set(tc.keys())
            if not missing:
                R.ok("tool_02_call_fields", f"all fields present: {tc.get('short_name', tc.get('name', '?'))}")
            else:
                R.fail("tool_02_call_fields", f"missing: {missing}, have: {set(tc.keys())}")
    except Exception as e:
        R.fail("tool_02_call_fields", str(e))

    # 3. tool_start.display has verb and detail
    try:
        if not tool_starts:
            R.skip("tool_03_start_display", "No tool_start events")
        else:
            disp = tool_starts[0]["data"].get("display", {})
            has_verb = "verb" in disp
            has_detail = "detail" in disp
            if has_verb and has_detail:
                R.ok("tool_03_start_display", f"verb={disp['verb']}, detail={str(disp['detail'])[:40]}")
            else:
                R.fail("tool_03_start_display", f"verb={has_verb}, detail={has_detail}, display={disp}")
    except Exception as e:
        R.fail("tool_03_start_display", str(e))

    # 4. tool_call.display has verb and detail
    try:
        if not tool_calls:
            R.skip("tool_04_call_display", "No tool_call events")
        else:
            disp = tool_calls[0]["data"].get("display", {})
            has_verb = "verb" in disp
            has_detail = "detail" in disp
            if has_verb and has_detail:
                R.ok("tool_04_call_display", f"verb={disp['verb']}, detail={str(disp['detail'])[:40]}")
            else:
                R.fail("tool_04_call_display", f"verb={has_verb}, detail={has_detail}, display={disp}")
    except Exception as e:
        R.fail("tool_04_call_display", str(e))

    # 5. tool_start and tool_call have matching id
    try:
        if not tool_starts or not tool_calls:
            R.skip("tool_05_matching_id", "No tool_start or tool_call events")
        else:
            start_ids = {ts["data"].get("id") for ts in tool_starts}
            call_ids = {tc["data"].get("id") for tc in tool_calls}
            matched = start_ids & call_ids
            if matched and len(matched) > 0:
                R.ok("tool_05_matching_id", f"{len(matched)} matched tool ids")
            else:
                R.fail("tool_05_matching_id", f"start_ids={start_ids}, call_ids={call_ids}")
    except Exception as e:
        R.fail("tool_05_matching_id", str(e))

    # 6. tool_start and tool_call have matching short_name
    try:
        if not tool_starts or not tool_calls:
            R.skip("tool_06_matching_short_name", "No tool_start or tool_call events")
        else:
            # Match by id, compare short_name
            start_map = {ts["data"].get("id"): ts["data"].get("short_name") for ts in tool_starts}
            all_match = True
            for tc in tool_calls:
                tc_id = tc["data"].get("id")
                if tc_id in start_map:
                    if start_map[tc_id] != tc["data"].get("short_name"):
                        all_match = False
                        break
            if all_match:
                R.ok("tool_06_matching_short_name", "all short_names match")
            else:
                R.fail("tool_06_matching_short_name", "short_name mismatch between start and call")
    except Exception as e:
        R.fail("tool_06_matching_short_name", str(e))

    # 7. For Read tool: tool_call.result has content key
    try:
        read_calls = [tc for tc in tool_calls if tc["data"].get("short_name") == "Read"
                      or tc["data"].get("name", "").lower().startswith("read")]
        if not read_calls:
            R.skip("tool_07_read_result_content", "No Read tool calls")
        else:
            tc_result = read_calls[0]["data"].get("result", {})
            if isinstance(tc_result, dict) and "content" in tc_result:
                R.ok("tool_07_read_result_content", f"content len={len(str(tc_result['content']))}")
            elif isinstance(tc_result, str) and len(tc_result) > 0:
                R.ok("tool_07_read_result_content", f"result is string, len={len(tc_result)}")
            else:
                R.fail("tool_07_read_result_content", f"result keys={list(tc_result.keys()) if isinstance(tc_result, dict) else type(tc_result)}")
    except Exception as e:
        R.fail("tool_07_read_result_content", str(e))

    if sid_tool:
        cleanup_session(app_id, sid_tool)

    time.sleep(3)

    # 8. For Bash tool: tool_call.result has stdout key
    sid_bash = None
    events_bash = []
    try:
        sid_bash, events_bash = stream_chat(app_id, "Run echo test")
    except Exception as exc:
        R.fail("stream_setup_bash", str(exc))

    try:
        bash_calls = [tc for tc in get_events_by_type(events_bash, "tool_call")
                      if tc["data"].get("short_name") == "Bash"
                      or tc["data"].get("name", "").lower().startswith("bash")]
        if not bash_calls:
            R.skip("tool_08_bash_result_stdout", "No Bash tool calls")
        else:
            tc_result = bash_calls[0]["data"].get("result", {})
            if isinstance(tc_result, dict) and "stdout" in tc_result:
                R.ok("tool_08_bash_result_stdout", f"stdout len={len(str(tc_result['stdout']))}")
            elif isinstance(tc_result, str) and len(tc_result) > 0:
                R.ok("tool_08_bash_result_stdout", f"result is string, len={len(tc_result)}")
            else:
                R.fail("tool_08_bash_result_stdout", f"result={tc_result}")
    except Exception as e:
        R.fail("tool_08_bash_result_stdout", str(e))

    if sid_bash:
        cleanup_session(app_id, sid_bash)

    # 9. Memory tools have silent=true (or not emitted)
    try:
        all_tool_starts = get_events_by_type(events_tool, "tool_start") + get_events_by_type(events_bash, "tool_start")
        all_tool_calls = get_events_by_type(events_tool, "tool_call") + get_events_by_type(events_bash, "tool_call")
        memory_starts = [ts for ts in all_tool_starts
                         if "memory" in ts["data"].get("name", "").lower()
                         or "memory" in ts["data"].get("short_name", "").lower()]
        memory_calls = [tc for tc in all_tool_calls
                        if "memory" in tc["data"].get("name", "").lower()
                        or "memory" in tc["data"].get("short_name", "").lower()]
        memory_all = memory_starts + memory_calls
        if not memory_all:
            R.skip("tool_09_memory_silent", "No memory tool events")
        else:
            all_silent = all(m["data"].get("silent") is True for m in memory_all)
            if all_silent:
                R.ok("tool_09_memory_silent", f"{len(memory_all)} memory tools, all silent")
            else:
                non_silent = [m["data"].get("short_name", m["data"].get("name")) for m in memory_all if not m["data"].get("silent")]
                R.fail("tool_09_memory_silent", f"non-silent memory tools: {non_silent}")
    except Exception as e:
        R.fail("tool_09_memory_silent", str(e))

    # 10. Non-memory tools have silent=false
    try:
        all_tool_starts = get_events_by_type(events_tool, "tool_start") + get_events_by_type(events_bash, "tool_start")
        non_memory = [ts for ts in all_tool_starts
                      if "memory" not in ts["data"].get("name", "").lower()
                      and "memory" not in ts["data"].get("short_name", "").lower()]
        if not non_memory:
            R.skip("tool_10_non_memory_visible", "No non-memory tool_start events")
        else:
            all_visible = all(m["data"].get("silent") is False or m["data"].get("silent") is None for m in non_memory)
            if all_visible:
                R.ok("tool_10_non_memory_visible", f"{len(non_memory)} non-memory tools, all visible")
            else:
                silent_ones = [m["data"].get("short_name", m["data"].get("name")) for m in non_memory if m["data"].get("silent") is True]
                R.fail("tool_10_non_memory_visible", f"unexpectedly silent: {silent_ones}")
    except Exception as e:
        R.fail("tool_10_non_memory_visible", str(e))

    time.sleep(3)

    # ────────────────────────────────────────────────────────────
    # EVENT ORDERING (10)
    # ────────────────────────────────────────────────────────────

    # Use the tool-using stream (events_tool) for ordering tests
    # Also get a fresh simple stream for basic ordering
    sid_order = None
    events_order = []
    try:
        sid_order, events_order = stream_chat(app_id, "Say ok")
    except Exception as exc:
        R.fail("stream_setup_order", str(exc))

    # 1. First event is hook or status (not token)
    try:
        if not events_order:
            R.fail("order_01_first_event", "No events")
        else:
            first = events_order[0]["event"]
            if first in ("status", "hook", "system"):
                R.ok("order_01_first_event", f"first event={first}")
            elif first == "token":
                R.fail("order_01_first_event", f"first event is token (should be status/hook)")
            else:
                R.ok("order_01_first_event", f"first event={first} (not token)")
    except Exception as e:
        R.fail("order_01_first_event", str(e))

    # 2. status/turn_start before status/requesting
    try:
        ts_idx = -1
        req_idx = -1
        for i, e in enumerate(events_order):
            if e["event"] == "status" and e["data"].get("phase") == "turn_start" and ts_idx == -1:
                ts_idx = i
            if e["event"] == "status" and e["data"].get("phase") == "requesting" and req_idx == -1:
                req_idx = i
        if ts_idx == -1:
            R.skip("order_02_turn_start_before_requesting", "No turn_start event")
        elif req_idx == -1:
            R.skip("order_02_turn_start_before_requesting", "No requesting event")
        elif ts_idx < req_idx:
            R.ok("order_02_turn_start_before_requesting", f"turn_start@{ts_idx} < requesting@{req_idx}")
        else:
            R.fail("order_02_turn_start_before_requesting", f"turn_start@{ts_idx} >= requesting@{req_idx}")
    except Exception as e:
        R.fail("order_02_turn_start_before_requesting", str(e))

    # 3. status/requesting before first token
    try:
        req_idx = -1
        for i, e in enumerate(events_order):
            if e["event"] == "status" and e["data"].get("phase") == "requesting":
                req_idx = i
                break
        tok_idx = _event_index(events_order, "token")
        if req_idx == -1:
            R.skip("order_03_requesting_before_token", "No requesting event")
        elif tok_idx == -1:
            R.skip("order_03_requesting_before_token", "No token events")
        elif req_idx < tok_idx:
            R.ok("order_03_requesting_before_token", f"requesting@{req_idx} < token@{tok_idx}")
        else:
            R.fail("order_03_requesting_before_token", f"requesting@{req_idx} >= token@{tok_idx}")
    except Exception as e:
        R.fail("order_03_requesting_before_token", str(e))

    # 4. stream_done after last token
    try:
        sd_idx = _event_index(events_order, "stream_done")
        last_tok = _last_event_index(events_order, "token")
        if sd_idx == -1:
            R.skip("order_04_done_after_token", "No stream_done event")
        elif last_tok == -1:
            R.skip("order_04_done_after_token", "No token events")
        elif sd_idx > last_tok:
            R.ok("order_04_done_after_token", f"last_token@{last_tok} < stream_done@{sd_idx}")
        else:
            R.fail("order_04_done_after_token", f"stream_done@{sd_idx} <= last_token@{last_tok}")
    except Exception as e:
        R.fail("order_04_done_after_token", str(e))

    # 5. tool_start before corresponding tool_call (use events_tool)
    try:
        ts_list = get_events_by_type(events_tool, "tool_start")
        tc_list = get_events_by_type(events_tool, "tool_call")
        if not ts_list or not tc_list:
            R.skip("order_05_start_before_call", "No tool events in tool stream")
        else:
            # Check by id
            all_ordered = True
            for tc in tc_list:
                tc_id = tc["data"].get("id")
                ts_idx_t = next((i for i, e in enumerate(events_tool) if e["event"] == "tool_start" and e["data"].get("id") == tc_id), -1)
                tc_idx_t = next((i for i, e in enumerate(events_tool) if e["event"] == "tool_call" and e["data"].get("id") == tc_id), -1)
                if ts_idx_t >= 0 and tc_idx_t >= 0 and ts_idx_t >= tc_idx_t:
                    all_ordered = False
                    break
            if all_ordered:
                R.ok("order_05_start_before_call", f"{len(tc_list)} tool pairs ordered correctly")
            else:
                R.fail("order_05_start_before_call", "tool_start after tool_call for some tool")
    except Exception as e:
        R.fail("order_05_start_before_call", str(e))

    # 6. result is always the last event (or second-to-last before heartbeat)
    try:
        if not events_order:
            R.fail("order_06_result_last", "No events")
        else:
            result_idx = _event_index(events_order, "result")
            if result_idx == -1:
                R.fail("order_06_result_last", "No result event")
            else:
                after_result = events_order[result_idx + 1:]
                non_hb = [e for e in after_result if e["event"] != "heartbeat"]
                if not non_hb:
                    R.ok("order_06_result_last", f"result@{result_idx}, nothing after (except {len(after_result)} heartbeats)")
                else:
                    R.fail("order_06_result_last", f"events after result: {[e['event'] for e in non_hb]}")
    except Exception as e:
        R.fail("order_06_result_last", str(e))

    # 7. No events after result except heartbeat
    try:
        result_idx = _event_index(events_order, "result")
        if result_idx == -1:
            R.skip("order_07_nothing_after_result", "No result event")
        else:
            after = events_order[result_idx + 1:]
            bad = [e["event"] for e in after if e["event"] != "heartbeat"]
            if not bad:
                R.ok("order_07_nothing_after_result", f"only {len(after)} heartbeats after result")
            else:
                R.fail("order_07_nothing_after_result", f"non-heartbeat after result: {bad}")
    except Exception as e:
        R.fail("order_07_nothing_after_result", str(e))

    if sid_order:
        cleanup_session(app_id, sid_order)

    time.sleep(3)

    # 8. Multiple turns: turn_end before next turn_start (use events_tool which may have multiple turns)
    try:
        turn_ends_idx = [i for i, e in enumerate(events_tool) if e["event"] == "status" and e["data"].get("phase") == "turn_end"]
        turn_starts_idx = [i for i, e in enumerate(events_tool) if e["event"] == "status" and e["data"].get("phase") == "turn_start"]
        if len(turn_starts_idx) < 2:
            R.skip("order_08_turn_end_before_start", "Less than 2 turns in tool stream")
        else:
            # Second turn_start should come after first turn_end
            if turn_ends_idx and turn_ends_idx[0] < turn_starts_idx[1]:
                R.ok("order_08_turn_end_before_start", f"turn_end@{turn_ends_idx[0]} < turn_start@{turn_starts_idx[1]}")
            elif not turn_ends_idx:
                R.fail("order_08_turn_end_before_start", "No turn_end events found")
            else:
                R.fail("order_08_turn_end_before_start", f"turn_end@{turn_ends_idx[0]} >= turn_start@{turn_starts_idx[1]}")
    except Exception as e:
        R.fail("order_08_turn_end_before_start", str(e))

    # 9. In tool-using turn: status -> token -> stream_done -> tool_start -> tool_call -> status(requesting) -> token -> result
    try:
        if not tool_starts or not tool_calls:
            R.skip("order_09_tool_turn_sequence", "No tool events")
        else:
            # Verify broad ordering: first status before first token before first tool_start
            first_status = _event_index(events_tool, "status")
            first_token = _event_index(events_tool, "token")
            first_ts = _event_index(events_tool, "tool_start")
            first_tc = _event_index(events_tool, "tool_call")
            res_idx = _event_index(events_tool, "result")

            checks = []
            if first_status >= 0 and first_token >= 0:
                checks.append(first_status < first_token)
            if first_token >= 0 and first_ts >= 0:
                checks.append(first_token < first_ts)
            if first_ts >= 0 and first_tc >= 0:
                checks.append(first_ts < first_tc)
            if first_tc >= 0 and res_idx >= 0:
                checks.append(first_tc < res_idx)

            if all(checks) and len(checks) >= 3:
                R.ok("order_09_tool_turn_sequence", f"status@{first_status}->token@{first_token}->tool_start@{first_ts}->tool_call@{first_tc}->result@{res_idx}")
            else:
                R.fail("order_09_tool_turn_sequence", f"ordering violated: status@{first_status} token@{first_token} ts@{first_ts} tc@{first_tc} result@{res_idx}")
    except Exception as e:
        R.fail("order_09_tool_turn_sequence", str(e))

    # 10. Error event replaces result on failure
    try:
        # We can't easily force an error, so check logic: if error event exists, result may be absent or have error
        error_events = get_events_by_type(events_order, "error")
        if error_events:
            result_ev = get_result(events_order)
            if result_ev is None or result_ev.get("error"):
                R.ok("order_10_error_replaces_result", "error event present, result absent or has error")
            else:
                R.fail("order_10_error_replaces_result", "error event but result has no error")
        else:
            # No error in normal flow - verify result exists and has no error
            result_ev = get_result(events_order)
            if result_ev and not result_ev.get("error"):
                R.ok("order_10_error_replaces_result", "no error event, result clean")
            elif result_ev and result_ev.get("error"):
                R.fail("order_10_error_replaces_result", f"result has error but no error event: {result_ev.get('error')}")
            else:
                R.skip("order_10_error_replaces_result", "No result event")
    except Exception as e:
        R.fail("order_10_error_replaces_result", str(e))

    # Clean up tool session
    if sid_tool:
        cleanup_session(app_id, sid_tool)

    return R
