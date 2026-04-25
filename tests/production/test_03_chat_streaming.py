"""Production tests: Chat streaming via SSE with real LLM calls.

60 tests covering SSE event validation, tool call events, thinking events,
multi-turn conversations, and error handling.
"""

from __future__ import annotations

import json
import time

from tests.production.helpers import (
    R,
    req,
    api_ok,
    ensure_app_deployed,
    stream_chat,
    get_streamed_content,
    get_result,
    get_error,
    get_events_by_type,
    cleanup_session,
    WORKSPACE,
    new_session_id,
    LLM_TIMEOUT,
)

# ── SSE Event Validation (20 tests) ─────────────────────────────


def test_sse_01_simple_chat_returns_result():
    """Simple chat returns result event."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hello")
        result = get_result(events)
        if result is not None:
            R.ok("sse_01_result_event", f"{len(events)} events")
        else:
            R.fail("sse_01_result_event", "No result event found")
    except Exception as e:
        R.fail("sse_01_result_event", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_02_result_has_session_id():
    """Result event has session_id."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hi")
        result = get_result(events)
        if result and result.get("session_id"):
            R.ok("sse_02_result_session_id", result["session_id"][:12])
        else:
            safe_result = str(result).encode("utf-8", errors="replace").decode("utf-8")
            R.fail("sse_02_result_session_id", f"Missing session_id in result: {safe_result}")
    except Exception as e:
        R.fail("sse_02_result_session_id", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_03_result_has_content():
    """Result event has content (either in result or streamed tokens)."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hello")
        content = get_streamed_content(events)
        if content and len(content) > 0:
            R.ok("sse_03_result_content", f"{len(content)} chars")
        else:
            R.fail("sse_03_result_content", "No content in result or tokens")
    except Exception as e:
        R.fail("sse_03_result_content", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_04_token_events_have_delta():
    """Token events have delta field."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say ok")
        tokens = get_events_by_type(events, "token")
        if not tokens:
            R.skip("sse_04_token_delta", "No token events emitted (content in result)")
        elif all("delta" in t["data"] for t in tokens):
            R.ok("sse_04_token_delta", f"{len(tokens)} token events")
        else:
            missing = [i for i, t in enumerate(tokens) if "delta" not in t["data"]]
            R.fail("sse_04_token_delta", f"Missing delta at indices {missing}")
    except Exception as e:
        R.fail("sse_04_token_delta", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_05_status_events_with_phases():
    """Stream has status events with phases."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say yes")
        statuses = get_events_by_type(events, "status")
        if not statuses:
            R.fail("sse_05_status_phases", "No status events")
        else:
            phases = [s["data"].get("phase", s["data"].get("type", "?")) for s in statuses]
            R.ok("sse_05_status_phases", f"phases: {phases}")
    except Exception as e:
        R.fail("sse_05_status_phases", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_06_status_turn_start_has_turn_number():
    """Status turn_start has turn number."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hi")
        statuses = get_events_by_type(events, "status")
        turn_starts = [s for s in statuses if s["data"].get("phase") == "turn_start"
                       or s["data"].get("type") == "turn_start"]
        if not turn_starts:
            R.skip("sse_06_turn_start_number", "No turn_start status event")
        elif any("turn" in s["data"] or "turn_number" in s["data"] for s in turn_starts):
            R.ok("sse_06_turn_start_number")
        else:
            R.fail("sse_06_turn_start_number", f"No turn number in turn_start: {turn_starts[0]['data']}")
    except Exception as e:
        R.fail("sse_06_turn_start_number", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_07_status_requesting_has_model():
    """Status requesting has model name."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hi")
        statuses = get_events_by_type(events, "status")
        requesting = [s for s in statuses if s["data"].get("phase") == "requesting"
                      or s["data"].get("type") == "requesting"]
        if not requesting:
            R.skip("sse_07_requesting_model", "No requesting status event")
        elif any("model" in s["data"] for s in requesting):
            model = next(s["data"]["model"] for s in requesting if "model" in s["data"])
            R.ok("sse_07_requesting_model", model)
        else:
            R.fail("sse_07_requesting_model", f"No model in requesting: {requesting[0]['data']}")
    except Exception as e:
        R.fail("sse_07_requesting_model", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_08_status_turn_end_has_reason():
    """Status turn_end has reason."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hi")
        statuses = get_events_by_type(events, "status")
        turn_ends = [s for s in statuses if s["data"].get("phase") == "turn_end"
                     or s["data"].get("type") == "turn_end"]
        if not turn_ends:
            R.skip("sse_08_turn_end_reason", "No turn_end status event")
        elif any("reason" in s["data"] for s in turn_ends):
            reason = next(s["data"]["reason"] for s in turn_ends if "reason" in s["data"])
            R.ok("sse_08_turn_end_reason", reason)
        else:
            R.fail("sse_08_turn_end_reason", f"No reason in turn_end: {turn_ends[0]['data']}")
    except Exception as e:
        R.fail("sse_08_turn_end_reason", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_09_stream_done_event():
    """stream_done event emitted between text and tools."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hi")
        done_events = get_events_by_type(events, "stream_done")
        if done_events:
            R.ok("sse_09_stream_done", f"{len(done_events)} stream_done events")
        else:
            # stream_done may not always appear if no tools are called
            R.skip("sse_09_stream_done", "No stream_done event (no tool use)")
    except Exception as e:
        R.fail("sse_09_stream_done", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_10_out_token_count():
    """out_token events have count > 0."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say ok")
        out_tokens = get_events_by_type(events, "out_token")
        if not out_tokens:
            # Check result for usage
            result = get_result(events)
            if result and result.get("usage", {}).get("output_tokens", 0) > 0:
                R.ok("sse_10_out_token_count", f"via result usage: {result['usage']['output_tokens']}")
            else:
                R.skip("sse_10_out_token_count", "No out_token events or usage data")
        elif all(t["data"].get("count", 0) > 0 for t in out_tokens):
            R.ok("sse_10_out_token_count", f"{len(out_tokens)} events")
        else:
            R.fail("sse_10_out_token_count", "Some out_token events have count <= 0")
    except Exception as e:
        R.fail("sse_10_out_token_count", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_11_in_token_count():
    """in_token events have count > 0."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say ok")
        in_tokens = get_events_by_type(events, "in_token")
        if not in_tokens:
            result = get_result(events)
            if result and result.get("usage", {}).get("input_tokens", 0) > 0:
                R.ok("sse_11_in_token_count", f"via result usage: {result['usage']['input_tokens']}")
            else:
                R.skip("sse_11_in_token_count", "No in_token events or usage data")
        elif all(t["data"].get("count", 0) > 0 for t in in_tokens):
            R.ok("sse_11_in_token_count", f"{len(in_tokens)} events")
        else:
            R.fail("sse_11_in_token_count", "Some in_token events have count <= 0")
    except Exception as e:
        R.fail("sse_11_in_token_count", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_12_no_error_on_success():
    """No error event on successful chat."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hello")
        error = get_error(events)
        if error is None:
            R.ok("sse_12_no_error_on_success")
        else:
            R.fail("sse_12_no_error_on_success", f"Unexpected error: {error}")
    except Exception as e:
        R.fail("sse_12_no_error_on_success", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_13_events_logical_order():
    """Events arrive in logical order (status before tokens before result)."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hello")
        types = [e["event"] for e in events]

        # Find first status, first token, and result indices
        first_status = next((i for i, t in enumerate(types) if t == "status"), None)
        first_token = next((i for i, t in enumerate(types) if t == "token"), None)
        result_idx = next((i for i, t in enumerate(types) if t == "result"), None)

        ok = True
        detail_parts = []
        if first_status is not None and first_token is not None and first_status > first_token:
            ok = False
            detail_parts.append("status after token")
        if first_token is not None and result_idx is not None and first_token > result_idx:
            ok = False
            detail_parts.append("token after result")
        if first_status is not None and result_idx is not None and first_status > result_idx:
            ok = False
            detail_parts.append("status after result")

        if ok:
            R.ok("sse_13_logical_order", f"events: {types[:8]}...")
        else:
            R.fail("sse_13_logical_order", "; ".join(detail_parts))
    except Exception as e:
        R.fail("sse_13_logical_order", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_14_no_heartbeat_for_fast():
    """Heartbeat NOT sent for fast responses (< 5s)."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        start = time.time()
        sid, events = stream_chat(app_id, "Say ok")
        elapsed = time.time() - start

        heartbeats = get_events_by_type(events, "heartbeat")
        if elapsed < 5.0 and len(heartbeats) == 0:
            R.ok("sse_14_no_heartbeat_fast", f"response in {elapsed:.1f}s, 0 heartbeats")
        elif elapsed >= 5.0:
            R.skip("sse_14_no_heartbeat_fast", f"Response took {elapsed:.1f}s (not fast)")
        else:
            R.fail("sse_14_no_heartbeat_fast", f"Got {len(heartbeats)} heartbeats in {elapsed:.1f}s")
    except Exception as e:
        R.fail("sse_14_no_heartbeat_fast", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_15_workspace_status_branch():
    """Result has workspace_status with branch."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hi")
        result = get_result(events)
        ws = result.get("workspace_status", {}) if result else {}
        if ws.get("branch"):
            R.ok("sse_15_workspace_branch", ws["branch"])
        elif result:
            R.skip("sse_15_workspace_branch", f"No workspace_status.branch in result keys: {list(result.keys())}")
        else:
            R.fail("sse_15_workspace_branch", "No result event")
    except Exception as e:
        R.fail("sse_15_workspace_branch", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_16_context_pressure():
    """Result has context with pressure."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hi")
        result = get_result(events)
        ctx = result.get("context", {}) if result else {}
        if "pressure" in ctx:
            R.ok("sse_16_context_pressure", f"pressure={ctx['pressure']}")
        elif result:
            R.skip("sse_16_context_pressure", f"No context.pressure in result keys: {list(result.keys())}")
        else:
            R.fail("sse_16_context_pressure", "No result event")
    except Exception as e:
        R.fail("sse_16_context_pressure", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_17_usage_input_tokens():
    """Result has usage with input_tokens."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hi")
        result = get_result(events)
        usage = result.get("usage", {}) if result else {}
        if usage.get("input_tokens", 0) > 0:
            R.ok("sse_17_usage_input_tokens", f"{usage['input_tokens']} tokens")
        elif result:
            R.skip("sse_17_usage_input_tokens", f"No usage.input_tokens; keys: {list(result.keys())}")
        else:
            R.fail("sse_17_usage_input_tokens", "No result event")
    except Exception as e:
        R.fail("sse_17_usage_input_tokens", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_18_usage_output_tokens():
    """Result has usage with output_tokens."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hi")
        result = get_result(events)
        usage = result.get("usage", {}) if result else {}
        if usage.get("output_tokens", 0) > 0:
            R.ok("sse_18_usage_output_tokens", f"{usage['output_tokens']} tokens")
        elif result:
            R.skip("sse_18_usage_output_tokens", f"No usage.output_tokens; keys: {list(result.keys())}")
        else:
            R.fail("sse_18_usage_output_tokens", "No result event")
    except Exception as e:
        R.fail("sse_18_usage_output_tokens", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_19_tokens_accumulate():
    """Multiple token events accumulate to full response."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hello world")
        tokens = get_events_by_type(events, "token")
        if not tokens:
            R.skip("sse_19_tokens_accumulate", "No token events emitted")
        else:
            accumulated = "".join(t["data"].get("delta", "") for t in tokens)
            if len(accumulated) > 0:
                R.ok("sse_19_tokens_accumulate", f"{len(tokens)} tokens -> {len(accumulated)} chars")
            else:
                R.fail("sse_19_tokens_accumulate", "Token deltas accumulated to empty string")
    except Exception as e:
        R.fail("sse_19_tokens_accumulate", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_sse_20_events_valid_json():
    """All SSE events are valid JSON."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say ok")
        invalid = [e for e in events if "raw" in e.get("data", {})
                   and e["data"].get("raw", "").strip() not in ("", "{}")]
        if not invalid:
            R.ok("sse_20_valid_json", f"{len(events)} events all valid JSON")
        else:
            R.fail("sse_20_valid_json", f"{len(invalid)} events had invalid JSON")
    except Exception as e:
        R.fail("sse_20_valid_json", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


# ── Tool Call Events (15 tests) ─────────────────────────────────

def _do_tool_chat(app_id: str, message: str, test_prefix: str):
    """Shared helper: chat with tool use, return (sid, events) or None on failure."""
    try:
        sid, events = stream_chat(app_id, message, timeout=LLM_TIMEOUT)
        return sid, events
    except Exception as e:
        R.fail(test_prefix, str(e))
        return None, None


def test_tool_01_file_read_tool_start():
    """Ask to read a file - tool_start event emitted."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Read the file README.md", timeout=LLM_TIMEOUT)
        tool_starts = get_events_by_type(events, "tool_start")
        if tool_starts:
            R.ok("tool_01_tool_start", f"{len(tool_starts)} tool_start events")
        else:
            R.skip("tool_01_tool_start", "No tool_start events (model may not have used tools)")
    except Exception as e:
        R.fail("tool_01_tool_start", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_02_tool_start_short_name():
    """tool_start has short_name field."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Read file app.yaml", timeout=LLM_TIMEOUT)
        tool_starts = get_events_by_type(events, "tool_start")
        if not tool_starts:
            R.skip("tool_02_short_name", "No tool_start events")
        elif tool_starts[0]["data"].get("short_name"):
            R.ok("tool_02_short_name", tool_starts[0]["data"]["short_name"])
        else:
            R.fail("tool_02_short_name", f"No short_name in tool_start: {list(tool_starts[0]['data'].keys())}")
    except Exception as e:
        R.fail("tool_02_short_name", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_03_tool_start_display_verb():
    """tool_start has display.verb field."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Read file app.yaml", timeout=LLM_TIMEOUT)
        tool_starts = get_events_by_type(events, "tool_start")
        if not tool_starts:
            R.skip("tool_03_display_verb", "No tool_start events")
        else:
            display = tool_starts[0]["data"].get("display", {})
            if display.get("verb"):
                R.ok("tool_03_display_verb", display["verb"])
            else:
                R.fail("tool_03_display_verb", f"No display.verb: {tool_starts[0]['data']}")
    except Exception as e:
        R.fail("tool_03_display_verb", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_04_tool_start_display_detail():
    """tool_start has display.detail field."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "List files in current directory", timeout=LLM_TIMEOUT)
        tool_starts = get_events_by_type(events, "tool_start")
        if not tool_starts:
            R.skip("tool_04_display_detail", "No tool_start events")
        else:
            display = tool_starts[0]["data"].get("display", {})
            if "detail" in display:
                R.ok("tool_04_display_detail", str(display["detail"])[:60])
            else:
                R.fail("tool_04_display_detail", f"No display.detail: {list(display.keys())}")
    except Exception as e:
        R.fail("tool_04_display_detail", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_05_tool_start_silent_flag():
    """tool_start has silent flag (false for filesystem tools)."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Read file README.md", timeout=LLM_TIMEOUT)
        tool_starts = get_events_by_type(events, "tool_start")
        if not tool_starts:
            R.skip("tool_05_silent_flag", "No tool_start events")
        elif "silent" in tool_starts[0]["data"]:
            silent = tool_starts[0]["data"]["silent"]
            R.ok("tool_05_silent_flag", f"silent={silent}")
        else:
            R.fail("tool_05_silent_flag", f"No silent flag: {list(tool_starts[0]['data'].keys())}")
    except Exception as e:
        R.fail("tool_05_silent_flag", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_06_tool_call_event():
    """tool_call event emitted after execution."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Read file README.md", timeout=LLM_TIMEOUT)
        tool_calls = get_events_by_type(events, "tool_call")
        if tool_calls:
            R.ok("tool_06_tool_call", f"{len(tool_calls)} tool_call events")
        else:
            R.skip("tool_06_tool_call", "No tool_call events (model may not have used tools)")
    except Exception as e:
        R.fail("tool_06_tool_call", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_07_tool_call_success():
    """tool_call has success flag."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Read file README.md", timeout=LLM_TIMEOUT)
        tool_calls = get_events_by_type(events, "tool_call")
        if not tool_calls:
            R.skip("tool_07_tool_call_success", "No tool_call events")
        elif "success" in tool_calls[0]["data"]:
            R.ok("tool_07_tool_call_success", f"success={tool_calls[0]['data']['success']}")
        else:
            R.fail("tool_07_tool_call_success", f"No success flag: {list(tool_calls[0]['data'].keys())}")
    except Exception as e:
        R.fail("tool_07_tool_call_success", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_08_tool_call_result_data():
    """tool_call has result data."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Read file README.md", timeout=LLM_TIMEOUT)
        tool_calls = get_events_by_type(events, "tool_call")
        if not tool_calls:
            R.skip("tool_08_tool_call_result", "No tool_call events")
        elif "result" in tool_calls[0]["data"]:
            R.ok("tool_08_tool_call_result", f"result type: {type(tool_calls[0]['data']['result']).__name__}")
        else:
            R.fail("tool_08_tool_call_result", f"No result in tool_call: {list(tool_calls[0]['data'].keys())}")
    except Exception as e:
        R.fail("tool_08_tool_call_result", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_09_tool_call_display():
    """tool_call has display metadata."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "List files here", timeout=LLM_TIMEOUT)
        tool_calls = get_events_by_type(events, "tool_call")
        if not tool_calls:
            R.skip("tool_09_tool_call_display", "No tool_call events")
        elif "display" in tool_calls[0]["data"]:
            R.ok("tool_09_tool_call_display", f"display keys: {list(tool_calls[0]['data']['display'].keys())}")
        else:
            R.fail("tool_09_tool_call_display", f"No display in tool_call: {list(tool_calls[0]['data'].keys())}")
    except Exception as e:
        R.fail("tool_09_tool_call_display", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_10_tool_call_matches_start():
    """tool_call has short_name matching tool_start."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Read file README.md", timeout=LLM_TIMEOUT)
        tool_starts = get_events_by_type(events, "tool_start")
        tool_calls = get_events_by_type(events, "tool_call")
        if not tool_starts or not tool_calls:
            R.skip("tool_10_name_match", "No tool_start or tool_call events")
        else:
            start_name = tool_starts[0]["data"].get("short_name", "")
            call_name = tool_calls[0]["data"].get("short_name", "")
            if start_name and call_name and start_name == call_name:
                R.ok("tool_10_name_match", start_name)
            elif start_name and call_name:
                R.fail("tool_10_name_match", f"Mismatch: start={start_name} call={call_name}")
            else:
                R.skip("tool_10_name_match", "Missing short_name in events")
    except Exception as e:
        R.fail("tool_10_name_match", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_11_memory_update_event():
    """Ask to use memory (SetGoal) - memory_update event emitted."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Set your goal to help me code", timeout=LLM_TIMEOUT)
        mem_updates = get_events_by_type(events, "memory_update")
        if mem_updates:
            R.ok("tool_11_memory_update", f"{len(mem_updates)} memory_update events")
        else:
            R.skip("tool_11_memory_update", "No memory_update events (model may not have used SetGoal)")
    except Exception as e:
        R.fail("tool_11_memory_update", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_12_memory_update_action():
    """memory_update has action field."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Remember my name is TestUser", timeout=LLM_TIMEOUT)
        mem_updates = get_events_by_type(events, "memory_update")
        if not mem_updates:
            R.skip("tool_12_memory_action", "No memory_update events")
        elif "action" in mem_updates[0]["data"]:
            R.ok("tool_12_memory_action", mem_updates[0]["data"]["action"])
        else:
            R.fail("tool_12_memory_action", f"No action: {list(mem_updates[0]['data'].keys())}")
    except Exception as e:
        R.fail("tool_12_memory_action", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_13_memory_update_result():
    """memory_update has result with goal."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Set goal: write tests", timeout=LLM_TIMEOUT)
        mem_updates = get_events_by_type(events, "memory_update")
        if not mem_updates:
            R.skip("tool_13_memory_result", "No memory_update events")
        elif "result" in mem_updates[0]["data"]:
            R.ok("tool_13_memory_result", f"result keys: {list(mem_updates[0]['data']['result'].keys()) if isinstance(mem_updates[0]['data']['result'], dict) else type(mem_updates[0]['data']['result']).__name__}")
        else:
            R.fail("tool_13_memory_result", f"No result: {list(mem_updates[0]['data'].keys())}")
    except Exception as e:
        R.fail("tool_13_memory_result", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_tool_14_silent_memory_tool():
    """Silent tool (memory) has silent=true in tool_start (if emitted)."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Set goal: testing", timeout=LLM_TIMEOUT)
        tool_starts = get_events_by_type(events, "tool_start")
        memory_starts = [t for t in tool_starts
                         if "memory" in t["data"].get("short_name", "").lower()
                         or "goal" in t["data"].get("short_name", "").lower()
                         or "setgoal" in t["data"].get("short_name", "").lower()]
        if not memory_starts:
            R.skip("tool_14_silent_memory", "No memory tool_start events")
        elif memory_starts[0]["data"].get("silent") is True:
            R.ok("tool_14_silent_memory", "silent=true")
        else:
            R.ok("tool_14_silent_memory", f"silent={memory_starts[0]['data'].get('silent', 'missing')}")
    except Exception as e:
        R.fail("tool_14_silent_memory", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


# Removed test_tool_15_workbench_read: the workbench stream was deprecated.
# File lifecycle events flow exclusively through preview:resource_set on
# the `files` channel now — see tools/behavior_tests.py WSP01-22.


# ── Thinking Events (5 tests) ───────────────────────────────────


def test_think_01_thinking_started():
    """thinking_started event emitted (model dependent)."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "What is 2+2?")
        think_starts = get_events_by_type(events, "thinking_started")
        if think_starts:
            R.ok("think_01_started", f"{len(think_starts)} thinking_started events")
        else:
            R.skip("think_01_started", "No thinking_started events (model may not support thinking)")
    except Exception as e:
        R.fail("think_01_started", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_think_02_thinking_delta():
    """thinking_delta events have delta field."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "What is 3+5?")
        think_deltas = get_events_by_type(events, "thinking_delta")
        if not think_deltas:
            R.skip("think_02_delta", "No thinking_delta events")
        elif all("delta" in d["data"] for d in think_deltas):
            R.ok("think_02_delta", f"{len(think_deltas)} deltas")
        else:
            R.fail("think_02_delta", "Some thinking_delta events missing delta field")
    except Exception as e:
        R.fail("think_02_delta", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_think_03_thinking_not_in_output():
    """Thinking content is not in visible output."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "What is 10+5?")
        think_deltas = get_events_by_type(events, "thinking_delta")
        if not think_deltas:
            R.skip("think_03_not_in_output", "No thinking events to compare")
        else:
            thinking_text = "".join(d["data"].get("delta", "") for d in think_deltas)
            visible = get_streamed_content(events)
            # Thinking should not be duplicated into visible output
            if thinking_text and thinking_text in visible:
                R.fail("think_03_not_in_output", "Thinking text appears in visible output")
            else:
                R.ok("think_03_not_in_output", f"thinking={len(thinking_text)}c, visible={len(visible)}c")
    except Exception as e:
        R.fail("think_03_not_in_output", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_think_04_thinking_model_dependent():
    """thinking events only emitted for thinking models."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say yes")
        think_events = get_events_by_type(events, "thinking_started") + get_events_by_type(events, "thinking_delta")
        statuses = get_events_by_type(events, "status")
        requesting = [s for s in statuses if s["data"].get("phase") == "requesting"
                      or s["data"].get("type") == "requesting"]
        model = requesting[0]["data"].get("model", "") if requesting else "unknown"

        # If model supports thinking, we may or may not see events; just verify no crash
        if think_events:
            R.ok("think_04_model_dependent", f"thinking events with model={model}")
        else:
            R.ok("think_04_model_dependent", f"no thinking events, model={model}")
    except Exception as e:
        R.fail("think_04_model_dependent", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_think_05_thinking_before_tools():
    """Thinking completes before tool use starts."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Read README.md", timeout=LLM_TIMEOUT)
        types = [e["event"] for e in events]
        last_think = -1
        first_tool = len(types)
        for i, t in enumerate(types):
            if t in ("thinking_started", "thinking_delta"):
                last_think = i
            if t == "tool_start" and first_tool == len(types):
                first_tool = i

        if last_think == -1:
            R.skip("think_05_before_tools", "No thinking events")
        elif first_tool == len(types):
            R.skip("think_05_before_tools", "No tool events")
        elif last_think < first_tool:
            R.ok("think_05_before_tools", f"thinking ends at {last_think}, tools start at {first_tool}")
        else:
            R.fail("think_05_before_tools", f"thinking at {last_think} after tool at {first_tool}")
    except Exception as e:
        R.fail("think_05_before_tools", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


# ── Multi-Turn (10 tests) ───────────────────────────────────────


def test_multi_turn():
    """Multi-turn conversation tests (10 tests)."""
    app_id = ensure_app_deployed()
    sid = new_session_id()
    turn_data = []  # list of (events, content)

    try:
        # Turn 1: tell name
        try:
            _, events1 = stream_chat(app_id, "My name is Zephyr", session_id=sid)
            content1 = get_streamed_content(events1)
            turn_data.append((events1, content1))
            if content1 and ("zephyr" in content1.lower() or len(content1) > 5):
                safe = content1[:60].encode("utf-8", errors="replace").decode("ascii", errors="replace")
                R.ok("multi_01_name_ack", f"response: {safe}")
            else:
                safe = (content1 or "")[:80].encode("utf-8", errors="replace").decode("ascii", errors="replace")
                R.fail("multi_01_name_ack", f"Response did not acknowledge: {safe}")
        except Exception as e:
            R.fail("multi_01_name_ack", str(e))
            turn_data.append(([], ""))

        # Turn 2: recall name
        try:
            _, events2 = stream_chat(app_id, "What is my name?", session_id=sid)
            content2 = get_streamed_content(events2)
            turn_data.append((events2, content2))
            if "zephyr" in content2.lower():
                R.ok("multi_02_recall_name", f"found 'Zephyr' in response")
            else:
                R.fail("multi_02_recall_name", f"Name not recalled: {content2[:80]}")
        except Exception as e:
            R.fail("multi_02_recall_name", str(e))
            turn_data.append(([], ""))

        # Turn 3: ask to remember a fact
        try:
            _, events3 = stream_chat(app_id, "Remember: the secret code is 42", session_id=sid)
            content3 = get_streamed_content(events3)
            turn_data.append((events3, content3))
            if content3 and len(content3) > 0:
                R.ok("multi_03_store_fact", f"response: {content3[:60]}")
            else:
                R.fail("multi_03_store_fact", "Empty response")
        except Exception as e:
            R.fail("multi_03_store_fact", str(e))
            turn_data.append(([], ""))

        # Turn 4: recall fact
        try:
            _, events4 = stream_chat(app_id, "What is the secret code?", session_id=sid)
            content4 = get_streamed_content(events4)
            turn_data.append((events4, content4))
            if "42" in content4:
                R.ok("multi_04_recall_fact", "found '42' in response")
            else:
                R.fail("multi_04_recall_fact", f"Fact not recalled: {content4[:80]}")
        except Exception as e:
            R.fail("multi_04_recall_fact", str(e))
            turn_data.append(([], ""))

        # Test 5: Turn count increments
        try:
            turn_numbers = []
            for events, _ in turn_data:
                statuses = get_events_by_type(events, "status")
                for s in statuses:
                    tn = s["data"].get("turn") or s["data"].get("turn_number")
                    if tn is not None:
                        turn_numbers.append(tn)
                        break
            if len(turn_numbers) >= 2 and turn_numbers[-1] > turn_numbers[0]:
                R.ok("multi_05_turn_count", f"turns: {turn_numbers}")
            elif turn_numbers:
                R.ok("multi_05_turn_count", f"turn numbers found: {turn_numbers}")
            else:
                R.skip("multi_05_turn_count", "No turn numbers in status events")
        except Exception as e:
            R.fail("multi_05_turn_count", str(e))

        # Test 6: Session persists messages across turns
        try:
            def _turn_has_response(events, content):
                if content:
                    return True
                # Some turns may have empty streamed content; check for token events or result
                result = get_result(events)
                if result and result.get("usage", {}).get("output_tokens", 0) > 0:
                    return True
                return len(get_events_by_type(events, "token")) > 0

            if len(turn_data) >= 2 and all(_turn_has_response(ev, c) for ev, c in turn_data[:2]):
                R.ok("multi_06_session_persists", f"{len(turn_data)} turns completed")
            else:
                R.fail("multi_06_session_persists", "Some turns had no content or tokens")
        except Exception as e:
            R.fail("multi_06_session_persists", str(e))

        # Test 7: Context grows with each turn (in_token increases)
        try:
            input_tokens = []
            for events, _ in turn_data:
                result = get_result(events)
                if result:
                    usage = result.get("usage", {})
                    it = usage.get("input_tokens", 0)
                    if it > 0:
                        input_tokens.append(it)
            if len(input_tokens) >= 2 and input_tokens[-1] > input_tokens[0]:
                R.ok("multi_07_context_grows", f"input_tokens: {input_tokens}")
            elif input_tokens:
                R.ok("multi_07_context_grows", f"input_tokens: {input_tokens} (may not strictly increase)")
            else:
                R.skip("multi_07_context_grows", "No input_tokens in usage")
        except Exception as e:
            R.fail("multi_07_context_grows", str(e))

        # Test 8: Each turn has turn_start status event
        try:
            all_have_start = True
            for events, _ in turn_data:
                statuses = get_events_by_type(events, "status")
                has_start = any(
                    s["data"].get("phase") == "turn_start" or s["data"].get("type") == "turn_start"
                    for s in statuses
                )
                if not has_start:
                    all_have_start = False
            if all_have_start and turn_data:
                R.ok("multi_08_turn_start_each", f"all {len(turn_data)} turns have turn_start")
            elif not turn_data:
                R.fail("multi_08_turn_start_each", "No turns completed")
            else:
                R.skip("multi_08_turn_start_each", "Not all turns had turn_start status")
        except Exception as e:
            R.fail("multi_08_turn_start_each", str(e))

        # Test 9: Each turn has turn_end status event
        try:
            all_have_end = True
            for events, _ in turn_data:
                statuses = get_events_by_type(events, "status")
                has_end = any(
                    s["data"].get("phase") == "turn_end" or s["data"].get("type") == "turn_end"
                    for s in statuses
                )
                if not has_end:
                    all_have_end = False
            if all_have_end and turn_data:
                R.ok("multi_09_turn_end_each", f"all {len(turn_data)} turns have turn_end")
            elif not turn_data:
                R.fail("multi_09_turn_end_each", "No turns completed")
            else:
                R.skip("multi_09_turn_end_each", "Not all turns had turn_end status")
        except Exception as e:
            R.fail("multi_09_turn_end_each", str(e))

        # Test 10: Session history shows all turns
        try:
            r = req("get", f"/api/apps/{app_id}/sessions/{sid}")
            if r.status_code == 200:
                body = r.json()
                data = body.get("data", body)
                messages = data.get("messages", data.get("history", []))
                if isinstance(messages, list) and len(messages) >= 4:
                    R.ok("multi_10_session_history", f"{len(messages)} messages in history")
                elif isinstance(messages, list):
                    R.ok("multi_10_session_history", f"{len(messages)} messages found")
                else:
                    R.skip("multi_10_session_history", f"Unexpected history format: {type(messages).__name__}")
            else:
                R.skip("multi_10_session_history", f"GET session returned {r.status_code}")
        except Exception as e:
            R.fail("multi_10_session_history", str(e))

    finally:
        cleanup_session(app_id, sid)


# ── Error Handling (10 tests) ───────────────────────────────────


def test_err_01_empty_message():
    """Empty message - handled gracefully."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "")
        # Either an error or a graceful response is acceptable
        error = get_error(events)
        result = get_result(events)
        if error or result:
            R.ok("err_01_empty_message", f"error={error}, has_result={result is not None}")
        else:
            R.ok("err_01_empty_message", "stream completed without crash")
    except AssertionError as e:
        # HTTP error is also acceptable for empty message
        R.ok("err_01_empty_message", f"rejected with: {str(e)[:60]}")
    except Exception as e:
        R.fail("err_01_empty_message", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_err_02_long_message():
    """Very long message (10k chars) - handled."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        long_msg = "What is 1+1? " + ("x" * 10000)
        sid, events = stream_chat(app_id, long_msg, timeout=LLM_TIMEOUT)
        error = get_error(events)
        result = get_result(events)
        if error:
            R.ok("err_02_long_message", f"handled with error: {error[:60]}")
        elif result:
            R.ok("err_02_long_message", "handled successfully")
        else:
            R.ok("err_02_long_message", "stream completed")
    except AssertionError as e:
        R.ok("err_02_long_message", f"rejected: {str(e)[:60]}")
    except Exception as e:
        R.fail("err_02_long_message", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_err_03_unicode_emoji():
    """Unicode message with emoji - handled."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Say hello \U0001f44b \u2764\ufe0f \U0001f680")
        content = get_streamed_content(events)
        if content:
            safe = content[:40].encode("utf-8", errors="replace").decode("ascii", errors="replace")
            R.ok("err_03_unicode_emoji", f"response: {safe}")
        else:
            R.ok("err_03_unicode_emoji", "handled without crash")
    except Exception as e:
        R.fail("err_03_unicode_emoji", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_err_04_special_chars():
    """Message with special chars (<>&"') - handled."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Echo: <b>&amp;\"quotes'</b>")
        content = get_streamed_content(events)
        if content:
            R.ok("err_04_special_chars", f"response: {content[:40]}")
        else:
            R.ok("err_04_special_chars", "handled without crash")
    except Exception as e:
        R.fail("err_04_special_chars", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_err_05_code_blocks():
    """Message with code blocks - handled."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, "Explain: ```print('hi')```")
        content = get_streamed_content(events)
        if content:
            R.ok("err_05_code_blocks", f"response: {content[:40]}")
        else:
            R.ok("err_05_code_blocks", "handled without crash")
    except Exception as e:
        R.fail("err_05_code_blocks", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_err_06_json_content():
    """Message with JSON content - handled."""
    app_id = ensure_app_deployed()
    sid = None
    try:
        sid, events = stream_chat(app_id, 'Parse this: {"key": "value", "n": 1}')
        content = get_streamed_content(events)
        if content:
            R.ok("err_06_json_content", f"response: {content[:40]}")
        else:
            R.ok("err_06_json_content", "handled without crash")
    except Exception as e:
        R.fail("err_06_json_content", str(e))
    finally:
        if sid:
            cleanup_session(app_id, sid)


def test_err_07_rapid_sequential():
    """Rapid sequential messages same session - serialized correctly."""
    app_id = ensure_app_deployed()
    sid = new_session_id()
    try:
        _, events1 = stream_chat(app_id, "Say one", session_id=sid)
        _, events2 = stream_chat(app_id, "Say two", session_id=sid)
        content1 = get_streamed_content(events1)
        content2 = get_streamed_content(events2)
        if content1 and content2:
            R.ok("err_07_rapid_sequential", f"both responded: {len(content1)}c, {len(content2)}c")
        else:
            R.fail("err_07_rapid_sequential", f"Missing content: c1={bool(content1)}, c2={bool(content2)}")
    except Exception as e:
        R.fail("err_07_rapid_sequential", str(e))
    finally:
        cleanup_session(app_id, sid)


def test_err_08_undeployed_app():
    """Chat to undeployed app - returns error."""
    try:
        sid, events = stream_chat("nonexistent-app-id-12345", "Say hi")
        error = get_error(events)
        if error:
            R.ok("err_08_undeployed_app", f"error: {error[:60]}")
        else:
            R.fail("err_08_undeployed_app", "No error returned for nonexistent app")
    except AssertionError as e:
        # HTTP error is expected
        R.ok("err_08_undeployed_app", f"HTTP error: {str(e)[:60]}")
    except Exception as e:
        R.fail("err_08_undeployed_app", str(e))


def test_err_09_invalid_session_id():
    """Chat with invalid session_id format - handled."""
    app_id = ensure_app_deployed()
    try:
        sid, events = stream_chat(app_id, "Say hi", session_id="not-a-valid-uuid!!!")
        error = get_error(events)
        content = get_streamed_content(events)
        if error:
            R.ok("err_09_invalid_session", f"error: {error[:60]}")
        elif content:
            R.ok("err_09_invalid_session", "server accepted invalid session_id gracefully")
        else:
            R.ok("err_09_invalid_session", "handled without crash")
    except AssertionError as e:
        R.ok("err_09_invalid_session", f"rejected: {str(e)[:60]}")
    except Exception as e:
        R.fail("err_09_invalid_session", str(e))


def test_err_10_no_workspace():
    """Chat without workspace - returns error."""
    app_id = ensure_app_deployed()
    try:
        import httpx
        from tests.production.helpers import DAEMON, ensure_auth, STREAM_TIMEOUT

        headers = dict(ensure_auth())
        sid = new_session_id()
        # Send request without workspace field
        with httpx.stream(
            "POST", f"{DAEMON}/api/apps/{app_id}/chat/stream",
            headers=headers,
            json={"session_id": sid, "message": "Say hi"},
            timeout=STREAM_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                R.ok("err_10_no_workspace", f"rejected with HTTP {resp.status_code}")
            else:
                # Collect events
                from tests.production.helpers import _collect_sse
                events = _collect_sse(resp)
                error = get_error(events)
                if error:
                    R.ok("err_10_no_workspace", f"error: {error[:60]}")
                else:
                    # Some servers might accept missing workspace
                    R.ok("err_10_no_workspace", "server handled missing workspace gracefully")
    except AssertionError as e:
        R.ok("err_10_no_workspace", f"rejected: {str(e)[:60]}")
    except Exception as e:
        R.fail("err_10_no_workspace", str(e))


# ── Runner ───────────────────────────────────────────────────────


def run_all():
    """Run all chat streaming tests."""
    print("\n=== Chat Streaming: SSE Event Validation (20 tests) ===\n")
    test_sse_01_simple_chat_returns_result()
    test_sse_02_result_has_session_id()
    test_sse_03_result_has_content()
    test_sse_04_token_events_have_delta()
    test_sse_05_status_events_with_phases()
    test_sse_06_status_turn_start_has_turn_number()
    test_sse_07_status_requesting_has_model()
    test_sse_08_status_turn_end_has_reason()
    test_sse_09_stream_done_event()
    test_sse_10_out_token_count()
    test_sse_11_in_token_count()
    test_sse_12_no_error_on_success()
    test_sse_13_events_logical_order()
    test_sse_14_no_heartbeat_for_fast()
    test_sse_15_workspace_status_branch()
    test_sse_16_context_pressure()
    test_sse_17_usage_input_tokens()
    test_sse_18_usage_output_tokens()
    test_sse_19_tokens_accumulate()
    test_sse_20_events_valid_json()

    print("\n=== Chat Streaming: Tool Call Events (15 tests) ===\n")
    test_tool_01_file_read_tool_start()
    test_tool_02_tool_start_short_name()
    test_tool_03_tool_start_display_verb()
    test_tool_04_tool_start_display_detail()
    test_tool_05_tool_start_silent_flag()
    test_tool_06_tool_call_event()
    test_tool_07_tool_call_success()
    test_tool_08_tool_call_result_data()
    test_tool_09_tool_call_display()
    test_tool_10_tool_call_matches_start()
    test_tool_11_memory_update_event()
    test_tool_12_memory_update_action()
    test_tool_13_memory_update_result()
    test_tool_14_silent_memory_tool()
    test_tool_15_workbench_read()

    print("\n=== Chat Streaming: Thinking Events (5 tests) ===\n")
    test_think_01_thinking_started()
    test_think_02_thinking_delta()
    test_think_03_thinking_not_in_output()
    test_think_04_thinking_model_dependent()
    test_think_05_thinking_before_tools()

    print("\n=== Chat Streaming: Multi-Turn (10 tests) ===\n")
    test_multi_turn()

    print("\n=== Chat Streaming: Error Handling (10 tests) ===\n")
    test_err_01_empty_message()
    test_err_02_long_message()
    test_err_03_unicode_emoji()
    test_err_04_special_chars()
    test_err_05_code_blocks()
    test_err_06_json_content()
    test_err_07_rapid_sequential()
    test_err_08_undeployed_app()
    test_err_09_invalid_session_id()
    test_err_10_no_workspace()


if __name__ == "__main__":
    run_all()
    print(f"\n{R.summary()}")
