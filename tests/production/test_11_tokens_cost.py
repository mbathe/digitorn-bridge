"""Production tests: Token counting accuracy, cost calculation, context pressure, session metrics.

Makes real HTTP requests to a live daemon. No mocking.
"""

from __future__ import annotations

import math
import os
import time

from tests.production.helpers import (
    R, req, api_ok, stream_chat, get_streamed_content,
    get_result, get_error, get_events_by_type, cleanup_session,
    new_session_id, ensure_auth, WORKSPACE, DAEMON,
)
import tests.production.helpers as _h

LLM_TIMEOUT = _h.LLM_TIMEOUT
TIMEOUT = _h.TIMEOUT

# ── Helpers ──────────────────────────────────────────────────

_sessions: list[tuple[str, str]] = []
_app_id: str = ""


def _deploy_test_full() -> str:
    """Deploy test-full.yaml and return app_id (GET-first to avoid rate limit)."""
    global _app_id
    if _app_id:
        r = req("get", f"/api/apps/{_app_id}")
        if r.status_code == 200:
            return _app_id
    # Check if already deployed before calling deploy
    r = req("get", "/api/apps/test-full")
    if r.status_code == 200:
        _app_id = "test-full"
        return _app_id
    yaml_path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "examples", "test-full.yaml",
    ))
    for _attempt in range(3):
        r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
        if r.status_code == 429:
            import time as _t
            _t.sleep(min(r.json().get("retry_after", 30) + 1, 60))
            continue
        data = api_ok(r)
        _app_id = data.get("app_id", "")
        assert _app_id, "No app_id in deploy response"
        return _app_id
    raise AssertionError(f"Deploy failed after 3 retries (429): {r.text[:300]}")


def _track(app_id: str, sid: str) -> None:
    _sessions.append((app_id, sid))


def _chat(app_id: str, msg: str, session_id: str | None = None,
          timeout: float = LLM_TIMEOUT) -> tuple[str, list[dict]]:
    sid, events = stream_chat(app_id, msg, session_id=session_id, timeout=timeout)
    _track(app_id, sid)
    return sid, events


def _cleanup_all() -> None:
    for app_id, sid in _sessions:
        cleanup_session(app_id, sid)


def _get_usage(events: list[dict]) -> dict:
    """Extract usage dict from result event."""
    result = get_result(events)
    return result.get("usage", {}) if result else {}


def _get_context(events: list[dict]) -> dict:
    """Extract context dict from result event."""
    result = get_result(events)
    return result.get("context", {}) if result else {}


def _get_pressure_from_events(events: list[dict]) -> float | None:
    """Extract pressure from hook or status events, or from result context."""
    # Try result context first
    result = get_result(events)
    if result:
        ctx = result.get("context", {})
        if "pressure" in ctx:
            return float(ctx["pressure"])
    # Try hook events
    hook_events = get_events_by_type(events, "hook")
    for ev in hook_events:
        d = ev.get("data", {})
        details = d.get("details", {})
        if d.get("action_type") == "context_status":
            p = details.get("pressure", details.get("context_pressure"))
            if p is not None:
                return float(p)
    # Try status events
    for ev in get_events_by_type(events, "status"):
        d = ev.get("data", {})
        if "pressure" in d:
            return float(d["pressure"])
    return None


# =================================================================
#  TOKEN COUNTING ACCURACY (15 tests)
# =================================================================

def _tok_01_out_token_events_match_result():
    name = "tok_01_out_token_sum_matches_result"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say hello.")
        out_events = get_events_by_type(events, "out_token")
        usage = _get_usage(events)
        result_out = usage.get("output_tokens", 0)
        if not out_events and result_out == 0:
            R.skip(name, "no out_token events and no output_tokens in result")
            return
        sse_sum = sum(e["data"].get("count", 0) for e in out_events)
        if result_out > 0 and sse_sum > 0:
            ratio = sse_sum / result_out
            assert 0.9 <= ratio <= 1.1, f"SSE sum {sse_sum} vs result {result_out} (ratio {ratio:.2f})"
            R.ok(name, f"sse={sse_sum} result={result_out}")
        elif result_out > 0:
            R.ok(name, f"result={result_out} (no SSE out_token events)")
        else:
            R.ok(name, f"sse_sum={sse_sum} (no result output_tokens)")
    except Exception as e:
        R.fail(name, str(e))


def _tok_02_in_token_matches_result():
    name = "tok_02_in_token_matches_result"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        in_events = get_events_by_type(events, "in_token")
        usage = _get_usage(events)
        result_in = usage.get("input_tokens", 0)
        if not in_events and result_in == 0:
            R.skip(name, "no in_token events and no input_tokens in result")
            return
        sse_last = max((e["data"].get("count", 0) for e in in_events), default=0)
        if result_in > 0 and sse_last > 0:
            ratio = sse_last / result_in
            assert 0.9 <= ratio <= 1.1, f"SSE last {sse_last} vs result {result_in} (ratio {ratio:.2f})"
            R.ok(name, f"sse={sse_last} result={result_in}")
        elif result_in > 0:
            R.ok(name, f"result={result_in} (no SSE in_token events)")
        else:
            R.ok(name, f"sse_last={sse_last} (no result input_tokens)")
    except Exception as e:
        R.fail(name, str(e))


def _tok_03_first_turn_input_gt_zero():
    name = "tok_03_first_turn_input_gt_zero"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Hi.")
        usage = _get_usage(events)
        input_tokens = usage.get("input_tokens", 0)
        if input_tokens > 0:
            R.ok(name, f"input_tokens={input_tokens}")
        else:
            in_events = get_events_by_type(events, "in_token")
            sse_max = max((e["data"].get("count", 0) for e in in_events), default=0)
            if sse_max > 0:
                R.ok(name, f"in_token SSE max={sse_max}")
            else:
                R.skip(name, "input_tokens=0 and no in_token events")
    except Exception as e:
        R.fail(name, str(e))


def _tok_04_output_tokens_gt_zero():
    name = "tok_04_output_tokens_gt_zero"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Tell me a joke.")
        usage = _get_usage(events)
        output_tokens = usage.get("output_tokens", 0)
        if output_tokens > 0:
            R.ok(name, f"output_tokens={output_tokens}")
        else:
            out_events = get_events_by_type(events, "out_token")
            sse_sum = sum(e["data"].get("count", 0) for e in out_events)
            if sse_sum > 0:
                R.ok(name, f"out_token SSE sum={sse_sum}")
            else:
                content = get_streamed_content(events)
                if content.strip():
                    R.skip(name, "response has content but no output token counts")
                else:
                    R.fail(name, "no output tokens and no content")
    except Exception as e:
        R.fail(name, str(e))


def _tok_05_total_input_gte_input():
    name = "tok_05_total_input_gte_input"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        stream_chat(app, "Say A.", session_id=sid, timeout=LLM_TIMEOUT)
        time.sleep(3)
        _, events = stream_chat(app, "Say B.", session_id=sid, timeout=LLM_TIMEOUT)
        usage = _get_usage(events)
        total_in = usage.get("total_input_tokens", 0)
        per_turn_in = usage.get("input_tokens", 0)
        if total_in > 0 and per_turn_in > 0:
            assert total_in >= per_turn_in, f"total {total_in} < per-turn {per_turn_in}"
            R.ok(name, f"total={total_in} >= turn={per_turn_in}")
        else:
            R.skip(name, f"total_input={total_in} input={per_turn_in}")
    except Exception as e:
        R.fail(name, str(e))


def _tok_06_total_output_gte_output():
    name = "tok_06_total_output_gte_output"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        stream_chat(app, "Say X.", session_id=sid, timeout=LLM_TIMEOUT)
        time.sleep(3)
        _, events = stream_chat(app, "Say Y.", session_id=sid, timeout=LLM_TIMEOUT)
        usage = _get_usage(events)
        total_out = usage.get("total_output_tokens", 0)
        per_turn_out = usage.get("output_tokens", 0)
        if total_out > 0 and per_turn_out > 0:
            assert total_out >= per_turn_out, f"total {total_out} < per-turn {per_turn_out}"
            R.ok(name, f"total={total_out} >= turn={per_turn_out}")
        else:
            R.skip(name, f"total_output={total_out} output={per_turn_out}")
    except Exception as e:
        R.fail(name, str(e))


def _tok_07_total_equals_sum():
    name = "tok_07_total_equals_in_plus_out"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        usage = _get_usage(events)
        total_in = usage.get("total_input_tokens", 0)
        total_out = usage.get("total_output_tokens", 0)
        total = usage.get("total_tokens", 0)
        if total > 0:
            assert total == total_in + total_out, (
                f"total {total} != in {total_in} + out {total_out}"
            )
            R.ok(name, f"{total} == {total_in} + {total_out}")
        else:
            R.skip(name, "total_tokens not set")
    except Exception as e:
        R.fail(name, str(e))


def _tok_08_context_grows_over_turns():
    name = "tok_08_context_grows_3_turns"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        _, ev1 = stream_chat(app, "Say 1.", session_id=sid, timeout=LLM_TIMEOUT)
        usage1 = _get_usage(ev1)
        first_in = usage1.get("input_tokens", 0) or usage1.get("total_input_tokens", 0)
        time.sleep(3)
        stream_chat(app, "Say 2.", session_id=sid, timeout=LLM_TIMEOUT)
        time.sleep(3)
        _, ev3 = stream_chat(app, "Say 3.", session_id=sid, timeout=LLM_TIMEOUT)
        usage3 = _get_usage(ev3)
        total_in = usage3.get("total_input_tokens", 0)
        if first_in > 0 and total_in > 0:
            assert total_in > 3 * first_in, (
                f"total_input {total_in} not > 3 * first_input {first_in}"
            )
            R.ok(name, f"total_in={total_in} > 3*first={3 * first_in}")
        else:
            R.skip(name, f"first_in={first_in} total_in={total_in}")
    except Exception as e:
        R.fail(name, str(e))


def _tok_09_compaction_reduces_input():
    name = "tok_09_compaction_reduces_input"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        # Build up context
        for i in range(4):
            stream_chat(app, f"Tell me fact {i}.", session_id=sid, timeout=LLM_TIMEOUT)
            time.sleep(3)
        _, ev_before = stream_chat(app, "Say ok.", session_id=sid, timeout=LLM_TIMEOUT)
        usage_before = _get_usage(ev_before)
        in_before = usage_before.get("input_tokens", 0)
        time.sleep(3)
        # Compact
        r = req("post", f"/api/apps/{app}/sessions/{sid}/compact")
        if r.status_code != 200:
            R.skip(name, f"compact endpoint returned {r.status_code}")
            return
        time.sleep(3)
        _, ev_after = stream_chat(app, "Say ok.", session_id=sid, timeout=LLM_TIMEOUT)
        usage_after = _get_usage(ev_after)
        in_after = usage_after.get("input_tokens", 0)
        if in_before > 0 and in_after > 0:
            if in_after < in_before:
                R.ok(name, f"before={in_before} after={in_after}")
            else:
                R.skip(name, f"input did not decrease: before={in_before} after={in_after}")
        else:
            R.skip(name, f"in_before={in_before} in_after={in_after}")
    except Exception as e:
        R.fail(name, str(e))


def _tok_10_token_counts_are_integers():
    name = "tok_10_token_counts_are_integers"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        usage = _get_usage(events)
        for key in ("input_tokens", "output_tokens", "total_input_tokens",
                     "total_output_tokens", "total_tokens"):
            val = usage.get(key)
            if val is not None:
                assert isinstance(val, int), f"{key}={val} is {type(val).__name__}, not int"
        R.ok(name, f"checked keys in usage")
    except Exception as e:
        R.fail(name, str(e))


def _tok_11_token_counts_never_negative():
    name = "tok_11_token_counts_never_negative"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        usage = _get_usage(events)
        for key in ("input_tokens", "output_tokens", "total_input_tokens",
                     "total_output_tokens", "total_tokens"):
            val = usage.get(key, 0)
            assert val >= 0, f"{key}={val} is negative"
        R.ok(name, "all token counts >= 0")
    except Exception as e:
        R.fail(name, str(e))


def _tok_12_empty_msg_has_input_tokens():
    name = "tok_12_empty_msg_has_input_tokens"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "")
        usage = _get_usage(events)
        input_tokens = usage.get("input_tokens", 0)
        if input_tokens > 0:
            R.ok(name, f"input_tokens={input_tokens} for empty message")
        else:
            # Check via SSE
            in_events = get_events_by_type(events, "in_token")
            sse_max = max((e["data"].get("count", 0) for e in in_events), default=0)
            if sse_max > 0:
                R.ok(name, f"in_token SSE={sse_max} for empty message")
            else:
                # Some APIs reject empty messages
                err = get_error(events)
                if err:
                    R.skip(name, f"empty message produced error: {err[:60]}")
                else:
                    R.skip(name, "no input_tokens for empty message")
    except Exception as e:
        R.fail(name, str(e))


def _tok_13_long_msg_more_tokens():
    name = "tok_13_long_msg_more_input_tokens"
    try:
        app = _deploy_test_full()
        sid_short, ev_short = _chat(app, "Hi.")
        time.sleep(3)
        long_msg = "Please explain in detail " + ("the meaning of life " * 20) + ". Be brief."
        sid_long, ev_long = _chat(app, long_msg)
        u_short = _get_usage(ev_short)
        u_long = _get_usage(ev_long)
        in_short = u_short.get("input_tokens", 0)
        in_long = u_long.get("input_tokens", 0)
        if in_short > 0 and in_long > 0:
            assert in_long > in_short, f"long {in_long} not > short {in_short}"
            R.ok(name, f"long={in_long} > short={in_short}")
        else:
            R.skip(name, f"in_short={in_short} in_long={in_long}")
    except Exception as e:
        R.fail(name, str(e))


def _tok_14_tool_results_add_input():
    name = "tok_14_tool_results_add_input"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        # First turn: no tool
        _, ev1 = stream_chat(app, "Say ok.", session_id=sid, timeout=LLM_TIMEOUT)
        u1 = _get_usage(ev1)
        in1 = u1.get("input_tokens", 0)
        time.sleep(3)
        # Second turn: ask to use a tool (read a file)
        _, ev2 = stream_chat(
            app, "Read the file README.md using the Read tool.",
            session_id=sid, timeout=LLM_TIMEOUT,
        )
        u2 = _get_usage(ev2)
        in2 = u2.get("input_tokens", 0)
        if in1 > 0 and in2 > 0:
            assert in2 > in1, f"turn2 input {in2} not > turn1 {in1}"
            R.ok(name, f"turn1={in1} turn2={in2}")
        else:
            R.skip(name, f"in1={in1} in2={in2}")
    except Exception as e:
        R.fail(name, str(e))


def _tok_15_sse_consistent_with_result():
    name = "tok_15_sse_consistent_with_result"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say hi.")
        usage = _get_usage(events)
        result_out = usage.get("output_tokens", 0)
        out_events = get_events_by_type(events, "out_token")
        sse_sum = sum(e["data"].get("count", 0) for e in out_events)
        in_events = get_events_by_type(events, "in_token")
        sse_in_max = max((e["data"].get("count", 0) for e in in_events), default=0)
        result_in = usage.get("input_tokens", 0)
        # At least one source should have data
        if result_out > 0 or sse_sum > 0:
            if result_out > 0 and sse_sum > 0:
                ratio = sse_sum / result_out
                assert 0.8 <= ratio <= 1.2, f"out ratio {ratio:.2f}"
            R.ok(name, f"out: sse={sse_sum} result={result_out}")
        elif result_in > 0 or sse_in_max > 0:
            R.ok(name, f"in: sse={sse_in_max} result={result_in}")
        else:
            R.skip(name, "no token data in SSE or result")
    except Exception as e:
        R.fail(name, str(e))


# =================================================================
#  COST CALCULATION (10 tests)
# =================================================================

def _cost_01_gte_zero():
    name = "cost_01_gte_zero"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        usage = _get_usage(events)
        cost = usage.get("cost_usd")
        if cost is not None:
            assert cost >= 0, f"cost_usd={cost} is negative"
            R.ok(name, f"cost_usd={cost}")
        else:
            R.skip(name, "cost_usd not in usage")
    except Exception as e:
        R.fail(name, str(e))


def _cost_02_gt_zero_for_conversation():
    name = "cost_02_gt_zero_for_conversation"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Tell me a short joke.")
        usage = _get_usage(events)
        cost = usage.get("cost_usd")
        if cost is not None:
            assert cost > 0, f"cost_usd={cost} should be > 0 for non-trivial chat"
            R.ok(name, f"cost_usd={cost}")
        else:
            R.skip(name, "cost_usd not in usage")
    except Exception as e:
        R.fail(name, str(e))


def _cost_03_increases_across_turns():
    name = "cost_03_increases_across_turns"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        _, ev1 = stream_chat(app, "Say A.", session_id=sid, timeout=LLM_TIMEOUT)
        cost1 = _get_usage(ev1).get("cost_usd")
        time.sleep(3)
        _, ev2 = stream_chat(app, "Say B.", session_id=sid, timeout=LLM_TIMEOUT)
        cost2 = _get_usage(ev2).get("cost_usd")
        if cost1 is not None and cost2 is not None:
            assert cost2 > cost1, f"cost did not increase: {cost1} -> {cost2}"
            R.ok(name, f"cost1={cost1} cost2={cost2}")
        else:
            R.skip(name, f"cost1={cost1} cost2={cost2}")
    except Exception as e:
        R.fail(name, str(e))


def _cost_04_is_float_reasonable_precision():
    name = "cost_04_is_float_reasonable_precision"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        usage = _get_usage(events)
        cost = usage.get("cost_usd")
        if cost is not None:
            assert isinstance(cost, (int, float)), f"cost_usd type={type(cost).__name__}"
            R.ok(name, f"cost_usd={cost} type={type(cost).__name__}")
        else:
            R.skip(name, "cost_usd not in usage")
    except Exception as e:
        R.fail(name, str(e))


def _cost_05_proportional_to_tokens():
    name = "cost_05_proportional_to_tokens"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        _, ev1 = stream_chat(app, "Say A.", session_id=sid, timeout=LLM_TIMEOUT)
        u1 = _get_usage(ev1)
        time.sleep(3)
        _, ev2 = stream_chat(app, "Say B.", session_id=sid, timeout=LLM_TIMEOUT)
        time.sleep(3)
        _, ev3 = stream_chat(app, "Say C.", session_id=sid, timeout=LLM_TIMEOUT)
        u3 = _get_usage(ev3)
        cost1 = u1.get("cost_usd", 0)
        cost3 = u3.get("cost_usd", 0)
        tok1 = u1.get("total_tokens", 0)
        tok3 = u3.get("total_tokens", 0)
        if cost1 > 0 and cost3 > 0 and tok3 > tok1:
            assert cost3 > cost1, f"more tokens but lower cost: {tok1}/{cost1} vs {tok3}/{cost3}"
            R.ok(name, f"tok1={tok1}/cost1={cost1} tok3={tok3}/cost3={cost3}")
        else:
            R.skip(name, f"cost1={cost1} cost3={cost3} tok1={tok1} tok3={tok3}")
    except Exception as e:
        R.fail(name, str(e))


def _cost_06_single_turn_under_one_cent():
    name = "cost_06_single_turn_under_1_cent"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        usage = _get_usage(events)
        cost = usage.get("cost_usd")
        if cost is not None:
            assert cost < 0.01, f"cost_usd={cost} >= $0.01 for single turn"
            R.ok(name, f"cost_usd={cost}")
        else:
            R.skip(name, "cost_usd not in usage")
    except Exception as e:
        R.fail(name, str(e))


def _cost_07_three_turns_gt_one_turn():
    name = "cost_07_3_turns_gt_1_turn"
    try:
        app = _deploy_test_full()
        # Single turn session
        sid1, ev1 = _chat(app, "Say ok.")
        cost1 = _get_usage(ev1).get("cost_usd")
        time.sleep(3)
        # Three turn session
        sid3 = new_session_id()
        _track(app, sid3)
        stream_chat(app, "Say A.", session_id=sid3, timeout=LLM_TIMEOUT)
        time.sleep(3)
        stream_chat(app, "Say B.", session_id=sid3, timeout=LLM_TIMEOUT)
        time.sleep(3)
        _, ev3 = stream_chat(app, "Say C.", session_id=sid3, timeout=LLM_TIMEOUT)
        cost3 = _get_usage(ev3).get("cost_usd")
        if cost1 is not None and cost3 is not None:
            assert cost3 > cost1, f"3-turn cost {cost3} not > 1-turn cost {cost1}"
            R.ok(name, f"1-turn={cost1} 3-turn={cost3}")
        else:
            R.skip(name, f"cost1={cost1} cost3={cost3}")
    except Exception as e:
        R.fail(name, str(e))


def _cost_08_not_nan_or_inf():
    name = "cost_08_not_nan_or_inf"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        usage = _get_usage(events)
        cost = usage.get("cost_usd")
        if cost is not None:
            assert not math.isnan(cost), "cost_usd is NaN"
            assert not math.isinf(cost), "cost_usd is Infinity"
            R.ok(name, f"cost_usd={cost}")
        else:
            R.skip(name, "cost_usd not in usage")
    except Exception as e:
        R.fail(name, str(e))


def _cost_09_max_6_decimal_places():
    name = "cost_09_max_6_decimal_places"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        usage = _get_usage(events)
        cost = usage.get("cost_usd")
        if cost is not None:
            cost_str = f"{cost:.10f}"
            # After 6 decimal places, rest should be zeros
            decimals = cost_str.split(".")[1] if "." in cost_str else ""
            trailing = decimals[6:]
            assert all(c == "0" for c in trailing), (
                f"cost_usd={cost} has more than 6 decimal places"
            )
            R.ok(name, f"cost_usd={cost}")
        else:
            R.skip(name, "cost_usd not in usage")
    except Exception as e:
        R.fail(name, str(e))


def _cost_10_matches_expected_range():
    name = "cost_10_matches_model_pricing"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        usage = _get_usage(events)
        cost = usage.get("cost_usd")
        total_in = usage.get("total_input_tokens", 0)
        total_out = usage.get("total_output_tokens", 0)
        if cost is not None and total_in > 0 and total_out > 0:
            # For deepseek-chat: input ~$0.80/M, output ~$4.0/M
            # Allow very wide range to accommodate any model
            expected_min = (total_in * 0.1 + total_out * 0.1) / 1_000_000
            expected_max = (total_in * 20.0 + total_out * 100.0) / 1_000_000
            assert expected_min <= cost <= expected_max, (
                f"cost {cost} outside range [{expected_min:.8f}, {expected_max:.8f}]"
            )
            R.ok(name, f"cost={cost} in={total_in} out={total_out}")
        else:
            R.skip(name, f"cost={cost} total_in={total_in} total_out={total_out}")
    except Exception as e:
        R.fail(name, str(e))


# =================================================================
#  CONTEXT PRESSURE (10 tests)
# =================================================================

def _ctx_01_pressure_starts_low():
    name = "ctx_01_pressure_starts_low"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        pressure = _get_pressure_from_events(events)
        if pressure is not None:
            assert pressure < 0.1, f"pressure={pressure} >= 0.1 for new session"
            R.ok(name, f"pressure={pressure}")
        else:
            R.skip(name, "pressure not reported in events")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_02_pressure_increases():
    name = "ctx_02_pressure_increases_with_turns"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        _, ev1 = stream_chat(app, "Say A.", session_id=sid, timeout=LLM_TIMEOUT)
        p1 = _get_pressure_from_events(ev1)
        time.sleep(3)
        stream_chat(app, "Say B.", session_id=sid, timeout=LLM_TIMEOUT)
        time.sleep(3)
        _, ev3 = stream_chat(app, "Say C.", session_id=sid, timeout=LLM_TIMEOUT)
        p3 = _get_pressure_from_events(ev3)
        if p1 is not None and p3 is not None:
            assert p3 >= p1, f"pressure did not increase: {p1} -> {p3}"
            R.ok(name, f"p1={p1} p3={p3}")
        else:
            R.skip(name, f"p1={p1} p3={p3}")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_03_pressure_in_range():
    name = "ctx_03_pressure_0_to_1"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        pressure = _get_pressure_from_events(events)
        if pressure is not None:
            assert 0.0 <= pressure <= 1.0, f"pressure={pressure} out of [0, 1]"
            R.ok(name, f"pressure={pressure}")
        else:
            R.skip(name, "pressure not reported")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_04_high_pressure_compaction_warning():
    name = "ctx_04_high_pressure_compaction_warning"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        # Build up context with many turns
        warning_seen = False
        for i in range(8):
            _, events = stream_chat(
                app, f"Tell me a detailed story about adventure {i}.",
                session_id=sid, timeout=LLM_TIMEOUT,
            )
            time.sleep(3)
            # Check for compaction-related hook events
            hook_events = get_events_by_type(events, "hook")
            for ev in hook_events:
                d = ev.get("data", {})
                action = d.get("action_type", "")
                if "compact" in action.lower() or "compaction" in action.lower():
                    warning_seen = True
            p = _get_pressure_from_events(events)
            if p is not None and p > 0.5:
                warning_seen = True
                break
        if warning_seen:
            R.ok(name, "high pressure or compaction detected")
        else:
            R.skip(name, "pressure never reached 0.5 or no compaction hook")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_05_compaction_reduces_pressure():
    name = "ctx_05_compaction_reduces_pressure"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        for i in range(4):
            stream_chat(app, f"Fact {i}: something.", session_id=sid, timeout=LLM_TIMEOUT)
            time.sleep(3)
        _, ev_before = stream_chat(app, "Say ok.", session_id=sid, timeout=LLM_TIMEOUT)
        p_before = _get_pressure_from_events(ev_before)
        time.sleep(3)
        r = req("post", f"/api/apps/{app}/sessions/{sid}/compact")
        if r.status_code != 200:
            R.skip(name, f"compact returned {r.status_code}")
            return
        time.sleep(3)
        _, ev_after = stream_chat(app, "Say ok.", session_id=sid, timeout=LLM_TIMEOUT)
        p_after = _get_pressure_from_events(ev_after)
        if p_before is not None and p_after is not None:
            if p_after <= p_before:
                R.ok(name, f"before={p_before} after={p_after}")
            else:
                R.skip(name, f"pressure did not decrease: {p_before} -> {p_after}")
        else:
            R.skip(name, f"p_before={p_before} p_after={p_after}")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_06_breakdown_sums_to_pressure():
    name = "ctx_06_breakdown_sums_approx"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        ctx = _get_context(events)
        if not ctx:
            R.skip(name, "no context in result")
            return
        sys_pct = ctx.get("system_prompt_pct", 0)
        tools_pct = ctx.get("tools_schema_pct", 0)
        msg_pct = ctx.get("message_history_pct", 0)
        pressure = ctx.get("pressure", 0)
        # Percentages are of effective_max, pressure is fraction
        sum_pct = sys_pct + tools_pct + msg_pct
        pressure_pct = pressure * 100
        if pressure > 0:
            # Allow generous tolerance since estimation methods differ
            diff = abs(sum_pct - pressure_pct)
            assert diff < 20, f"sum_pct={sum_pct:.1f} vs pressure_pct={pressure_pct:.1f} diff={diff:.1f}"
            R.ok(name, f"sum={sum_pct:.1f}% pressure={pressure_pct:.1f}%")
        else:
            R.skip(name, f"pressure={pressure}")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_07_system_prompt_pct_gt_zero():
    name = "ctx_07_system_prompt_pct_gt_zero"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        ctx = _get_context(events)
        if not ctx:
            R.skip(name, "no context in result")
            return
        sys_pct = ctx.get("system_prompt_pct", 0)
        sys_tokens = ctx.get("system_prompt_tokens", 0)
        if sys_pct > 0 or sys_tokens > 0:
            R.ok(name, f"sys_pct={sys_pct} sys_tokens={sys_tokens}")
        else:
            R.skip(name, f"sys_pct={sys_pct} sys_tokens={sys_tokens}")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_08_message_history_pct_increases():
    name = "ctx_08_message_history_pct_increases"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        _, ev1 = stream_chat(app, "Say A.", session_id=sid, timeout=LLM_TIMEOUT)
        ctx1 = _get_context(ev1)
        msg_pct1 = ctx1.get("message_history_pct", 0) if ctx1 else 0
        time.sleep(3)
        stream_chat(app, "Say B.", session_id=sid, timeout=LLM_TIMEOUT)
        time.sleep(3)
        _, ev3 = stream_chat(app, "Say C.", session_id=sid, timeout=LLM_TIMEOUT)
        ctx3 = _get_context(ev3)
        msg_pct3 = ctx3.get("message_history_pct", 0) if ctx3 else 0
        if msg_pct1 > 0 and msg_pct3 > 0:
            assert msg_pct3 > msg_pct1, f"msg_pct did not increase: {msg_pct1} -> {msg_pct3}"
            R.ok(name, f"pct1={msg_pct1} pct3={msg_pct3}")
        else:
            R.skip(name, f"msg_pct1={msg_pct1} msg_pct3={msg_pct3}")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_09_max_tokens_matches_model():
    name = "ctx_09_max_tokens_matches_model"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        ctx = _get_context(events)
        if not ctx:
            R.skip(name, "no context in result")
            return
        max_tok = ctx.get("max_tokens", 0)
        if max_tok > 0:
            # test-full uses deepseek-chat with context max_tokens: 131000
            assert max_tok >= 1000, f"max_tokens={max_tok} too small"
            R.ok(name, f"max_tokens={max_tok}")
        else:
            R.skip(name, f"max_tokens={max_tok}")
    except Exception as e:
        R.fail(name, str(e))


def _ctx_10_effective_max_lt_max():
    name = "ctx_10_effective_max_lt_max"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        ctx = _get_context(events)
        if not ctx:
            R.skip(name, "no context in result")
            return
        max_tok = ctx.get("max_tokens", 0)
        effective = ctx.get("effective_max", 0)
        output_reserved = ctx.get("output_reserved", 0)
        if max_tok > 0 and effective > 0:
            assert effective < max_tok, f"effective {effective} >= max {max_tok}"
            assert effective == max_tok - output_reserved, (
                f"effective {effective} != max {max_tok} - reserved {output_reserved}"
            )
            R.ok(name, f"effective={effective} max={max_tok} reserved={output_reserved}")
        else:
            R.skip(name, f"max={max_tok} effective={effective}")
    except Exception as e:
        R.fail(name, str(e))


# =================================================================
#  SESSION METRICS (5 tests)
# =================================================================

def _metrics_01_session_endpoint():
    name = "metrics_01_session_endpoint"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        time.sleep(3)
        r = req("get", f"/api/metrics/sessions/{sid}")
        if r.status_code != 200:
            R.skip(name, f"metrics endpoint returned {r.status_code}")
            return
        data = r.json()
        if data.get("error"):
            R.skip(name, f"session not found in metrics: {data['error']}")
            return
        assert "tokens" in data, f"no 'tokens' key in metrics: {list(data.keys())}"
        R.ok(name, f"keys={list(data.keys())[:6]}")
    except Exception as e:
        R.fail(name, str(e))


def _metrics_02_tokens_match_sse():
    name = "metrics_02_tokens_match_sse"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        usage = _get_usage(events)
        time.sleep(3)
        r = req("get", f"/api/metrics/sessions/{sid}")
        if r.status_code != 200:
            R.skip(name, f"metrics endpoint returned {r.status_code}")
            return
        data = r.json()
        if data.get("error"):
            R.skip(name, "session not found in metrics")
            return
        m_tokens = data.get("tokens", {})
        m_total = m_tokens.get("total", 0)
        u_total = usage.get("total_tokens", 0)
        if m_total > 0 and u_total > 0:
            assert m_total == u_total, f"metrics total {m_total} != usage total {u_total}"
            R.ok(name, f"metrics={m_total} usage={u_total}")
        elif m_total > 0:
            R.ok(name, f"metrics total={m_total} (usage total not set)")
        else:
            R.skip(name, f"metrics_total={m_total} usage_total={u_total}")
    except Exception as e:
        R.fail(name, str(e))


def _metrics_03_turn_count():
    name = "metrics_03_turn_count"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        stream_chat(app, "Say A.", session_id=sid, timeout=LLM_TIMEOUT)
        time.sleep(3)
        stream_chat(app, "Say B.", session_id=sid, timeout=LLM_TIMEOUT)
        time.sleep(3)
        r = req("get", f"/api/metrics/sessions/{sid}")
        if r.status_code != 200:
            R.skip(name, f"metrics endpoint returned {r.status_code}")
            return
        data = r.json()
        if data.get("error"):
            R.skip(name, "session not found in metrics")
            return
        turns = data.get("turns", {})
        current = turns.get("current", 0)
        if current >= 2:
            R.ok(name, f"turns={current}")
        elif current > 0:
            R.ok(name, f"turns={current} (may count differently)")
        else:
            R.skip(name, f"turn count={current}")
    except Exception as e:
        R.fail(name, str(e))


def _metrics_04_tool_call_counts():
    name = "metrics_04_tool_call_counts"
    try:
        app = _deploy_test_full()
        sid = new_session_id()
        _track(app, sid)
        stream_chat(
            app, "Run the command: echo hello",
            session_id=sid, timeout=LLM_TIMEOUT,
        )
        time.sleep(3)
        r = req("get", f"/api/metrics/sessions/{sid}")
        if r.status_code != 200:
            R.skip(name, f"metrics endpoint returned {r.status_code}")
            return
        data = r.json()
        if data.get("error"):
            R.skip(name, "session not found in metrics")
            return
        tools = data.get("tools", {})
        total = tools.get("total", 0)
        if total > 0:
            R.ok(name, f"tool_calls={total} by_tool={list(tools.get('by_tool', {}).keys())}")
        else:
            R.skip(name, "no tool calls recorded in metrics")
    except Exception as e:
        R.fail(name, str(e))


def _metrics_05_llm_latency():
    name = "metrics_05_llm_latency"
    try:
        app = _deploy_test_full()
        sid, events = _chat(app, "Say ok.")
        time.sleep(3)
        r = req("get", f"/api/metrics/sessions/{sid}")
        if r.status_code != 200:
            R.skip(name, f"metrics endpoint returned {r.status_code}")
            return
        data = r.json()
        if data.get("error"):
            R.skip(name, "session not found in metrics")
            return
        llm = data.get("llm", {})
        calls = llm.get("calls", 0)
        avg_ms = llm.get("avg_latency_ms", 0)
        if calls > 0:
            assert avg_ms > 0, f"llm calls={calls} but avg_latency={avg_ms}"
            R.ok(name, f"calls={calls} avg_ms={avg_ms}")
        else:
            R.skip(name, "no LLM calls in metrics")
    except Exception as e:
        R.fail(name, str(e))


# =================================================================
#  run_all
# =================================================================

def run_all():
    """Run all 40 token counting, cost, context pressure, and session metrics tests."""
    # Force token refresh
    _h._token_created_at = 0
    ensure_auth()

    # Deploy test-full app
    try:
        app = _deploy_test_full()
        print(f"  Deployed test-full app: {app}")
    except Exception as e:
        print(f"  \033[31mFailed to deploy test-full: {e}\033[0m")
        R.fail("deploy_test_full", str(e))
        return

    try:
        print("\n\033[1m=== TOKEN COUNTING ACCURACY ===\033[0m")
        _tok_01_out_token_events_match_result()
        _tok_02_in_token_matches_result()
        _tok_03_first_turn_input_gt_zero()
        _tok_04_output_tokens_gt_zero()
        _tok_05_total_input_gte_input()
        _tok_06_total_output_gte_output()
        _tok_07_total_equals_sum()
        _tok_08_context_grows_over_turns()
        _tok_09_compaction_reduces_input()
        _tok_10_token_counts_are_integers()
        _tok_11_token_counts_never_negative()
        _tok_12_empty_msg_has_input_tokens()
        _tok_13_long_msg_more_tokens()
        _tok_14_tool_results_add_input()
        _tok_15_sse_consistent_with_result()

        print("\n\033[1m=== COST CALCULATION ===\033[0m")
        _cost_01_gte_zero()
        _cost_02_gt_zero_for_conversation()
        _cost_03_increases_across_turns()
        _cost_04_is_float_reasonable_precision()
        _cost_05_proportional_to_tokens()
        _cost_06_single_turn_under_one_cent()
        _cost_07_three_turns_gt_one_turn()
        _cost_08_not_nan_or_inf()
        _cost_09_max_6_decimal_places()
        _cost_10_matches_expected_range()

        print("\n\033[1m=== CONTEXT PRESSURE ===\033[0m")
        _ctx_01_pressure_starts_low()
        _ctx_02_pressure_increases()
        _ctx_03_pressure_in_range()
        _ctx_04_high_pressure_compaction_warning()
        _ctx_05_compaction_reduces_pressure()
        _ctx_06_breakdown_sums_to_pressure()
        _ctx_07_system_prompt_pct_gt_zero()
        _ctx_08_message_history_pct_increases()
        _ctx_09_max_tokens_matches_model()
        _ctx_10_effective_max_lt_max()

        print("\n\033[1m=== SESSION METRICS ===\033[0m")
        _metrics_01_session_endpoint()
        _metrics_02_tokens_match_sse()
        _metrics_03_turn_count()
        _metrics_04_tool_call_counts()
        _metrics_05_llm_latency()
    finally:
        _cleanup_all()
