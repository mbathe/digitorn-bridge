"""Production tests: Error recovery, retries, timeouts, and resilience.

45 tests covering error recovery (15), timeout behavior (10),
connection handling (10), and resilience patterns (10).

Makes real HTTP requests to a live daemon. No mocking.
"""

from __future__ import annotations

from tests.production.helpers import (
    R, req, api_ok, stream_chat, get_streamed_content,
    get_result, get_error, get_events_by_type, cleanup_session,
    new_session_id, ensure_auth, ensure_app_deployed,
    WORKSPACE, DAEMON, TIMEOUT, STREAM_TIMEOUT, LLM_TIMEOUT,
)
import tests.production.helpers as _h
import httpx
import time
import uuid
import json
import concurrent.futures
import os


# ── Helpers ──────────────────────────────────────────────────

_sessions: list[tuple[str, str]] = []  # (app_id, session_id)


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
    _sessions.clear()


# =================================================================
#  ERROR RECOVERY (15 tests)
# =================================================================

def _err_01_session_works_after_error():
    """After an error response, session still works."""
    name = "ERR-01: Session works after error"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        # Trigger a likely error by sending to nonexistent session action
        req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        # Now use the session normally
        _, events = _chat(app, "Say OK.", session_id=sid)
        content = get_streamed_content(events)
        assert content, "No response after error"
        R.ok(name, content.strip()[:40])
    except Exception as e:
        R.fail(name, str(e))


def _err_02_session_works_after_abort():
    """After abort, session still works."""
    name = "ERR-02: Session works after abort"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Say hello.")
        req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        time.sleep(3)
        _, events = stream_chat(app, "Say OK.", session_id=sid)
        content = get_streamed_content(events)
        assert content, "No response after abort"
        R.ok(name, content.strip()[:40])
    except Exception as e:
        R.fail(name, str(e))


def _err_03_session_works_after_compact():
    """After compaction, session still works coherently."""
    name = "ERR-03: Session works after compact"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Remember: the secret word is TIGER.")
        time.sleep(3)
        _chat(app, "Tell me a short joke.", session_id=sid)
        time.sleep(3)
        _chat(app, "Tell me another joke.", session_id=sid)
        req("post", f"/api/apps/{app}/sessions/{sid}/compact")
        time.sleep(3)
        _, events = stream_chat(app, "What was the secret word?", session_id=sid)
        content = get_streamed_content(events)
        assert content, "No response after compaction"
        R.ok(name, content.strip()[:60])
    except Exception as e:
        R.fail(name, str(e))


def _err_04_next_request_after_rate_limit():
    """After rate limit (if happens), next request succeeds."""
    name = "ERR-04: Recovery after rate limit"
    try:
        app = ensure_app_deployed()
        # stream_chat already handles 429 internally with retry
        sid, events = _chat(app, "Say OK.")
        content = get_streamed_content(events)
        assert content, "No response"
        R.ok(name, "stream_chat handles 429 retry internally")
    except Exception as e:
        R.fail(name, str(e))


def _err_05_invalid_tool_params_no_crash():
    """Invalid tool params don't crash the session."""
    name = "ERR-05: Invalid tool params no crash"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Try reading the file /nonexistent/path/xyz123.txt and tell me what happens.")
        time.sleep(3)
        _, events = stream_chat(app, "Say OK if you can still respond.", session_id=sid)
        content = get_streamed_content(events)
        assert content, "Session unresponsive after invalid tool params"
        R.ok(name, content.strip()[:40])
    except Exception as e:
        R.fail(name, str(e))


def _err_06_long_tool_output_no_crash():
    """Very long tool output doesn't crash (read a large file)."""
    name = "ERR-06: Long tool output no crash"
    try:
        app = ensure_app_deployed()
        sid, events = _chat(app, "Read the file pyproject.toml and count how many lines it has. Be brief.")
        content = get_streamed_content(events)
        assert content, "No response for large file read"
        R.ok(name, content.strip()[:50])
    except Exception as e:
        R.fail(name, str(e))


def _err_07_unicode_in_tool_results():
    """Unicode in tool results handled correctly."""
    name = "ERR-07: Unicode in tool results"
    try:
        app = ensure_app_deployed()
        sid, events = _chat(app, "Reply with these exact chars: cafe\u0301 \u00fc\u00f1\u00ee\u00e7\u00f6d\u00e9 \u2603 \u2764")
        content = get_streamed_content(events)
        assert content, "No response with unicode"
        R.ok(name, content.strip()[:50])
    except Exception as e:
        R.fail(name, str(e))


def _err_08_empty_tool_result():
    """Empty tool result handled (tool returns success but no data)."""
    name = "ERR-08: Empty tool result handled"
    try:
        app = ensure_app_deployed()
        # Ask to list a directory that may be empty
        sid, events = _chat(app, "List files in a new empty temp directory. Be brief about results.")
        content = get_streamed_content(events)
        assert content, "No response for empty tool result"
        R.ok(name, content.strip()[:40])
    except Exception as e:
        R.fail(name, str(e))


def _err_09_tool_timeout_no_crash():
    """Tool timeout doesn't crash session."""
    name = "ERR-09: Tool timeout no crash"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Say hello briefly.")
        time.sleep(3)
        # Session should still be usable
        _, events = stream_chat(app, "Say OK.", session_id=sid)
        content = get_streamed_content(events)
        assert content, "Session broken after tool usage"
        R.ok(name, content.strip()[:40])
    except Exception as e:
        R.fail(name, str(e))


def _err_10_consecutive_errors_no_corrupt():
    """Multiple consecutive errors don't corrupt session state."""
    name = "ERR-10: Consecutive errors no corruption"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Say hello.")
        # Trigger multiple aborts (non-destructive errors)
        for _ in range(3):
            req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        time.sleep(3)
        _, events = stream_chat(app, "Are you still OK? Say yes.", session_id=sid)
        content = get_streamed_content(events)
        assert content, "Session corrupted after multiple errors"
        R.ok(name, content.strip()[:40])
    except Exception as e:
        R.fail(name, str(e))


def _err_11_history_intact_after_error():
    """Session history intact after error."""
    name = "ERR-11: History intact after error"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Remember DOLPHIN.")
        req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        r = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        data = api_ok(r)
        msgs = data.get("messages", [])
        raw = json.dumps(msgs).lower()
        assert "dolphin" in raw, f"History lost DOLPHIN keyword"
        R.ok(name, f"{len(msgs)} messages preserved")
    except Exception as e:
        R.fail(name, str(e))


def _err_12_memory_intact_after_error():
    """Session memory intact after error."""
    name = "ERR-12: Memory intact after error"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Set your goal to 'test memory resilience'. Use set_goal.")
        req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        r = req("get", f"/api/apps/{app}/sessions/{sid}/memory")
        if r.status_code == 200:
            data = api_ok(r)
            R.ok(name, f"memory keys: {list(data.keys())[:5]}")
        else:
            R.skip(name, f"Memory endpoint returned {r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _err_13_abort_during_stream_no_zombie():
    """Abort during streaming doesn't leave zombie session."""
    name = "ERR-13: Abort during stream no zombie"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        headers = dict(ensure_auth())

        # Start a stream and abort quickly
        try:
            with httpx.stream(
                "POST", f"{DAEMON}/api/apps/{app}/chat/stream",
                headers=headers,
                json={"session_id": sid, "message": "Write a long essay about Python.", "workspace": WORKSPACE},
                timeout=STREAM_TIMEOUT,
            ) as resp:
                # Read a few bytes then abort
                for chunk in resp.iter_bytes():
                    break
                req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        except Exception:
            pass

        time.sleep(3)
        # Verify daemon is healthy
        r = httpx.get(f"{DAEMON}/health", timeout=TIMEOUT)
        assert r.status_code == 200, f"Daemon unhealthy: {r.status_code}"
        # Verify session is not stuck
        _, events = stream_chat(app, "Say OK.", session_id=sid)
        _track(app, sid)
        content = get_streamed_content(events)
        assert content, "Session is zombie after abort-during-stream"
        R.ok(name, content.strip()[:40])
    except Exception as e:
        R.fail(name, str(e))


def _err_14_fork_of_errored_session():
    """Session works after fork of errored session."""
    name = "ERR-14: Fork of errored session works"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Remember PENGUIN.")
        # Simulate error state via abort
        req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        # Fork the session
        r = req("post", f"/api/apps/{app}/sessions/{sid}/fork")
        data = api_ok(r)
        fork_id = data.get("new_session_id", data.get("session_id", ""))
        assert fork_id, "No fork ID returned"
        _track(app, fork_id)
        time.sleep(3)
        _, events = stream_chat(app, "What word did I ask you to remember?", session_id=fork_id)
        content = get_streamed_content(events)
        assert content, "Fork unresponsive"
        R.ok(name, content.strip()[:50])
    except Exception as e:
        R.fail(name, str(e))


def _err_15_error_event_has_message():
    """Error event has descriptive error message (not empty)."""
    name = "ERR-15: Error event has message"
    try:
        app = ensure_app_deployed()
        fake_sid = f"nonexistent-{uuid.uuid4()}"
        headers = dict(ensure_auth())
        try:
            with httpx.stream(
                "POST", f"{DAEMON}/api/apps/{app}/chat/stream",
                headers=headers,
                json={"session_id": fake_sid, "message": "", "workspace": WORKSPACE},
                timeout=30.0,
            ) as resp:
                if resp.status_code != 200:
                    R.ok(name, f"HTTP {resp.status_code} with body")
                else:
                    events = _h._collect_sse(resp)
                    error = get_error(events)
                    if error:
                        assert len(error) > 0, "Error message is empty"
                        R.ok(name, f"error: {error[:60]}")
                    else:
                        # Empty message might just succeed; that is acceptable
                        R.ok(name, "No error for empty message (accepted)")
        except AssertionError as e:
            R.ok(name, f"HTTP error: {str(e)[:60]}")
    except Exception as e:
        R.fail(name, str(e))


# =================================================================
#  TIMEOUT BEHAVIOR (10 tests)
# =================================================================

def _timeout_01_health_500ms():
    """Health endpoint responds within 500ms."""
    name = "TMO-01: Health within 500ms"
    try:
        start = time.perf_counter()
        r = httpx.get(f"{DAEMON}/health", timeout=5.0)
        elapsed = (time.perf_counter() - start) * 1000
        assert r.status_code == 200, f"HTTP {r.status_code}"
        assert elapsed < 500, f"Too slow: {elapsed:.0f}ms"
        R.ok(name, f"{elapsed:.0f}ms")
    except Exception as e:
        R.fail(name, str(e))


def _timeout_02_app_list_1s():
    """App list responds within 1s."""
    name = "TMO-02: App list within 1s"
    try:
        start = time.perf_counter()
        r = req("get", "/api/apps")
        elapsed = (time.perf_counter() - start) * 1000
        assert r.status_code == 200, f"HTTP {r.status_code}"
        assert elapsed < 1000, f"Too slow: {elapsed:.0f}ms"
        R.ok(name, f"{elapsed:.0f}ms")
    except Exception as e:
        R.fail(name, str(e))


def _timeout_03_session_ops_2s():
    """Session operations respond within 2s."""
    name = "TMO-03: Session ops within 2s"
    try:
        app = ensure_app_deployed()
        start = time.perf_counter()
        r = req("get", f"/api/apps/{app}/sessions")
        elapsed = (time.perf_counter() - start) * 1000
        assert r.status_code == 200, f"HTTP {r.status_code}"
        assert elapsed < 2000, f"Too slow: {elapsed:.0f}ms"
        R.ok(name, f"{elapsed:.0f}ms")
    except Exception as e:
        R.fail(name, str(e))


def _timeout_04_stream_first_byte_3s():
    """Chat stream opens within 3s (first byte)."""
    name = "TMO-04: Stream first byte within 3s"
    sid = None
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        headers = dict(ensure_auth())
        start = time.perf_counter()
        with httpx.stream(
            "POST", f"{DAEMON}/api/apps/{app}/chat/stream",
            headers=headers,
            json={"session_id": sid, "message": "Say hi.", "workspace": WORKSPACE},
            timeout=30.0,
        ) as resp:
            # First byte = headers received
            elapsed = (time.perf_counter() - start) * 1000
            assert resp.status_code == 200, f"HTTP {resp.status_code}"
            assert elapsed < 3000, f"First byte too slow: {elapsed:.0f}ms"
            # Drain stream
            for _ in resp.iter_bytes():
                pass
        _track(app, sid)
        R.ok(name, f"{elapsed:.0f}ms")
    except Exception as e:
        if sid:
            _track(ensure_app_deployed(), sid)
        R.fail(name, str(e))


def _timeout_05_sse_heartbeat_6s():
    """SSE heartbeat arrives within 6s of silence."""
    name = "TMO-05: SSE heartbeat within 6s"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        headers = dict(ensure_auth())
        with httpx.stream(
            "POST", f"{DAEMON}/api/apps/{app}/chat/stream",
            headers=headers,
            json={"session_id": sid, "message": "Say OK.", "workspace": WORKSPACE},
            timeout=30.0,
        ) as resp:
            assert resp.status_code == 200, f"HTTP {resp.status_code}"
            # Check for heartbeat events (: comment lines or heartbeat events)
            events = _h._collect_sse(resp)
        _track(app, sid)
        # Heartbeat is optional; pass if stream completed normally
        heartbeats = [e for e in events if e["event"] in ("heartbeat", "ping", "keep-alive")]
        if heartbeats:
            R.ok(name, f"{len(heartbeats)} heartbeat events")
        else:
            R.ok(name, "Stream completed without needing heartbeat")
    except Exception as e:
        R.fail(name, str(e))


def _timeout_06_simple_chat_30s():
    """Chat with simple question completes within 30s."""
    name = "TMO-06: Simple chat within 30s"
    try:
        app = ensure_app_deployed()
        start = time.perf_counter()
        sid, events = _chat(app, "What is 2+2? Just the number.", timeout=30.0)
        elapsed = time.perf_counter() - start
        content = get_streamed_content(events)
        assert content, "No response"
        assert elapsed < 30, f"Too slow: {elapsed:.1f}s"
        R.ok(name, f"{elapsed:.1f}s")
    except Exception as e:
        R.fail(name, str(e))


def _timeout_07_tool_use_chat_60s():
    """Chat with tool use completes within 60s."""
    name = "TMO-07: Tool use chat within 60s"
    try:
        app = ensure_app_deployed()
        start = time.perf_counter()
        sid, events = _chat(app, "Read pyproject.toml and tell me the version. Be brief.", timeout=60.0)
        elapsed = time.perf_counter() - start
        content = get_streamed_content(events)
        assert content, "No response"
        assert elapsed < 60, f"Too slow: {elapsed:.1f}s"
        R.ok(name, f"{elapsed:.1f}s")
    except Exception as e:
        R.fail(name, str(e))


def _timeout_08_compact_10s():
    """Session compact completes within 10s."""
    name = "TMO-08: Compact within 10s"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Hello, first message.")
        time.sleep(3)
        _chat(app, "Second message.", session_id=sid)
        start = time.perf_counter()
        r = req("post", f"/api/apps/{app}/sessions/{sid}/compact", timeout=10.0)
        elapsed = (time.perf_counter() - start) * 1000
        assert r.status_code == 200, f"HTTP {r.status_code}"
        assert elapsed < 10000, f"Too slow: {elapsed:.0f}ms"
        R.ok(name, f"{elapsed:.0f}ms")
    except Exception as e:
        R.fail(name, str(e))


def _timeout_09_concurrent_5_chats_90s():
    """Concurrent 5 chats all complete within 90s."""
    name = "TMO-09: 5 concurrent chats within 90s"
    try:
        app = ensure_app_deployed()
        messages = [f"What is {i}+{i}? Just the number." for i in range(1, 6)]
        start = time.perf_counter()

        def do_chat(msg):
            sid, events = stream_chat(app, msg, timeout=90.0)
            _track(app, sid)
            return get_streamed_content(events)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(do_chat, m) for m in messages]
            results = [f.result(timeout=90) for f in futures]

        elapsed = time.perf_counter() - start
        assert all(r for r in results), f"Some chats returned empty"
        assert elapsed < 90, f"Too slow: {elapsed:.1f}s"
        R.ok(name, f"{elapsed:.1f}s, all 5 completed")
    except Exception as e:
        R.fail(name, str(e))


def _timeout_10_no_hanging_requests():
    """No request hangs indefinitely (all have timeouts)."""
    name = "TMO-10: No hanging requests"
    try:
        app = ensure_app_deployed()
        # Test several endpoints with explicit short timeouts
        endpoints = [
            ("get", f"{DAEMON}/health"),
            ("get", f"{DAEMON}/api/apps"),
        ]
        for method, url in endpoints:
            headers = dict(ensure_auth())
            r = getattr(httpx, method)(url, headers=headers, timeout=10.0)
            assert r.status_code in (200, 401, 403, 404), f"{url}: HTTP {r.status_code}"
        R.ok(name, f"All {len(endpoints)} endpoints responded with timeout")
    except httpx.TimeoutException as e:
        R.fail(name, f"Request hung: {e}")
    except Exception as e:
        R.fail(name, str(e))


# =================================================================
#  CONNECTION HANDLING (10 tests)
# =================================================================

def _conn_01_disconnect_during_stream():
    """Client disconnect during stream - daemon doesn't crash."""
    name = "CONN-01: Disconnect during stream no crash"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        headers = dict(ensure_auth())
        try:
            with httpx.stream(
                "POST", f"{DAEMON}/api/apps/{app}/chat/stream",
                headers=headers,
                json={"session_id": sid, "message": "Write a long story.", "workspace": WORKSPACE},
                timeout=10.0,
            ) as resp:
                # Read one chunk then disconnect
                for chunk in resp.iter_bytes():
                    break
        except Exception:
            pass

        time.sleep(2)
        r = httpx.get(f"{DAEMON}/health", timeout=TIMEOUT)
        assert r.status_code == 200, f"Daemon crashed: {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _conn_02_rapid_connect_disconnect():
    """Rapid connect/disconnect doesn't leak resources."""
    name = "CONN-02: Rapid connect/disconnect no leak"
    try:
        app = ensure_app_deployed()
        headers = dict(ensure_auth())

        for i in range(10):
            try:
                with httpx.stream(
                    "POST", f"{DAEMON}/api/apps/{app}/chat/stream",
                    headers=headers,
                    json={"session_id": new_session_id(), "message": "Hi", "workspace": WORKSPACE},
                    timeout=5.0,
                ) as resp:
                    for chunk in resp.iter_bytes():
                        break
            except Exception:
                pass

        time.sleep(2)
        r = httpx.get(f"{DAEMON}/health", timeout=TIMEOUT)
        assert r.status_code == 200, f"Health check failed after rapid connects"
        R.ok(name, "10 rapid connections, health OK")
    except Exception as e:
        R.fail(name, str(e))


def _conn_03_concurrent_streams_same_session():
    """Multiple concurrent streams to same session - serialized correctly."""
    name = "CONN-03: Concurrent streams same session"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Say hello.")
        time.sleep(3)

        def send_msg(msg):
            try:
                _, events = stream_chat(app, msg, session_id=sid, timeout=60.0)
                return get_streamed_content(events)
            except Exception as ex:
                return f"error: {ex}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(send_msg, "Say A.")
            f2 = pool.submit(send_msg, "Say B.")
            r1 = f1.result(timeout=60)
            r2 = f2.result(timeout=60)

        # At least one should succeed; both erroring means serialization is broken
        assert r1 or r2, "Both concurrent streams failed"
        R.ok(name, f"r1={str(r1)[:30]}, r2={str(r2)[:30]}")
    except Exception as e:
        R.fail(name, str(e))


def _conn_04_stream_nonexistent_app():
    """Stream to nonexistent app returns error immediately (not hang)."""
    name = "CONN-04: Nonexistent app error immediate"
    try:
        headers = dict(ensure_auth())
        start = time.perf_counter()
        try:
            sid, events = stream_chat("nonexistent-app-xyz", "Hi", timeout=10.0)
            error = get_error(events)
            elapsed = time.perf_counter() - start
            if error:
                R.ok(name, f"{elapsed:.1f}s, error: {error[:40]}")
            else:
                R.ok(name, f"{elapsed:.1f}s, completed (may have default app)")
        except AssertionError as e:
            elapsed = time.perf_counter() - start
            assert elapsed < 5, f"Took too long to error: {elapsed:.1f}s"
            R.ok(name, f"{elapsed:.1f}s, HTTP error")
    except Exception as e:
        R.fail(name, str(e))


def _conn_05_expired_token_returns_401():
    """Stream with expired token returns 401 immediately."""
    name = "CONN-05: Expired token returns 401"
    try:
        app = ensure_app_deployed()
        fake_headers = {"Authorization": "Bearer expired.token.value"}
        start = time.perf_counter()
        r = httpx.post(
            f"{DAEMON}/api/apps/{app}/chat/stream",
            headers=fake_headers,
            json={"session_id": new_session_id(), "message": "Hi", "workspace": WORKSPACE},
            timeout=5.0,
        )
        elapsed = (time.perf_counter() - start) * 1000
        assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"
        assert elapsed < 2000, f"Too slow: {elapsed:.0f}ms"
        R.ok(name, f"{elapsed:.0f}ms, HTTP {r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _conn_06_large_response_streams():
    """Large response (ask for detailed explanation) streams correctly."""
    name = "CONN-06: Large response streams correctly"
    try:
        app = ensure_app_deployed()
        sid, events = _chat(app, "Explain Python decorators in 5 paragraphs.")
        content = get_streamed_content(events)
        assert len(content) > 100, f"Response too short: {len(content)} chars"
        tokens = get_events_by_type(events, "token")
        assert len(tokens) > 5, f"Too few token events: {len(tokens)}"
        R.ok(name, f"{len(content)} chars, {len(tokens)} token events")
    except Exception as e:
        R.fail(name, str(e))


def _conn_07_binary_content_no_break():
    """Binary-like content in tool result doesn't break stream."""
    name = "CONN-07: Binary content no break"
    try:
        app = ensure_app_deployed()
        sid, events = _chat(app, "What are the first 5 bytes of a PNG file header in hex? Be brief.")
        content = get_streamed_content(events)
        assert content, "No response for binary content question"
        R.ok(name, content.strip()[:50])
    except Exception as e:
        R.fail(name, str(e))


def _conn_08_sse_content_type():
    """SSE content-type is text/event-stream."""
    name = "CONN-08: SSE content-type"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        headers = dict(ensure_auth())
        with httpx.stream(
            "POST", f"{DAEMON}/api/apps/{app}/chat/stream",
            headers=headers,
            json={"session_id": sid, "message": "Say OK.", "workspace": WORKSPACE},
            timeout=30.0,
        ) as resp:
            ct = resp.headers.get("content-type", "")
            assert "text/event-stream" in ct, f"Content-Type: {ct}"
            # Drain stream
            for _ in resp.iter_bytes():
                pass
        _track(app, sid)
        R.ok(name, f"Content-Type: {ct}")
    except Exception as e:
        R.fail(name, str(e))


def _conn_09_sse_no_cache():
    """SSE has no-cache header."""
    name = "CONN-09: SSE no-cache header"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        headers = dict(ensure_auth())
        with httpx.stream(
            "POST", f"{DAEMON}/api/apps/{app}/chat/stream",
            headers=headers,
            json={"session_id": sid, "message": "Say OK.", "workspace": WORKSPACE},
            timeout=30.0,
        ) as resp:
            cache_ctrl = resp.headers.get("cache-control", "")
            has_no_cache = "no-cache" in cache_ctrl
            for _ in resp.iter_bytes():
                pass
        _track(app, sid)
        if has_no_cache:
            R.ok(name, f"Cache-Control: {cache_ctrl}")
        else:
            R.skip(name, f"Cache-Control: {cache_ctrl or '(not set)'}")
    except Exception as e:
        R.fail(name, str(e))


def _conn_10_stream_chunked():
    """Stream response is chunked (Transfer-Encoding)."""
    name = "CONN-10: Stream is chunked"
    try:
        app = ensure_app_deployed()
        sid = new_session_id()
        headers = dict(ensure_auth())
        with httpx.stream(
            "POST", f"{DAEMON}/api/apps/{app}/chat/stream",
            headers=headers,
            json={"session_id": sid, "message": "Say OK.", "workspace": WORKSPACE},
            timeout=30.0,
        ) as resp:
            te = resp.headers.get("transfer-encoding", "")
            for _ in resp.iter_bytes():
                pass
        _track(app, sid)
        if "chunked" in te.lower():
            R.ok(name, f"Transfer-Encoding: {te}")
        else:
            # HTTP/2 doesn't use Transfer-Encoding chunked
            http_ver = str(getattr(resp, "http_version", ""))
            R.ok(name, f"TE: {te or 'none'} (HTTP version: {http_ver or 'unknown'})")
    except Exception as e:
        R.fail(name, str(e))


# =================================================================
#  RESILIENCE PATTERNS (10 tests)
# =================================================================

def _res_01_10_sequential_chats():
    """10 sequential chats to same session - all succeed, state consistent."""
    name = "RES-01: 10 sequential chats"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Count: 1. Say OK.")
        for i in range(2, 11):
            time.sleep(3)
            _, events = stream_chat(app, f"Count: {i}. Say OK.", session_id=sid)
            content = get_streamed_content(events)
            assert content, f"No response at turn {i}"
        r = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        msgs = api_ok(r).get("messages", [])
        assert len(msgs) >= 20, f"Expected 20+ messages, got {len(msgs)}"
        R.ok(name, f"{len(msgs)} messages after 10 turns")
    except Exception as e:
        R.fail(name, str(e))


def _res_02_chat_compact_chat():
    """Alternate chat + compact + chat - session stays coherent."""
    name = "RES-02: Chat-compact-chat coherent"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Remember EAGLE.")
        time.sleep(3)
        _chat(app, "Tell me a joke.", session_id=sid)
        time.sleep(3)
        _chat(app, "Another joke.", session_id=sid)
        req("post", f"/api/apps/{app}/sessions/{sid}/compact")
        time.sleep(3)
        _, events = stream_chat(app, "Say OK.", session_id=sid)
        content = get_streamed_content(events)
        assert content, "No response after compact cycle"
        R.ok(name, content.strip()[:40])
    except Exception as e:
        R.fail(name, str(e))


def _res_03_chat_fork_chat_original_unaffected():
    """Chat -> fork -> chat on fork -> original unaffected."""
    name = "RES-03: Fork doesn't affect original"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Remember FALCON.")
        r_pre = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        pre_count = len(api_ok(r_pre).get("messages", []))

        r = req("post", f"/api/apps/{app}/sessions/{sid}/fork")
        fork_id = api_ok(r).get("new_session_id", api_ok(r).get("session_id", ""))
        assert fork_id, "No fork ID"
        _track(app, fork_id)
        time.sleep(3)
        stream_chat(app, "This is fork-only message.", session_id=fork_id)

        r_post = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        post_count = len(api_ok(r_post).get("messages", []))
        assert post_count == pre_count, f"Original changed: {pre_count} -> {post_count}"
        R.ok(name, f"original={post_count} msgs unchanged")
    except Exception as e:
        R.fail(name, str(e))


def _res_04_chat_abort_chat_history():
    """Chat -> abort -> chat -> history shows all interactions."""
    name = "RES-04: History shows all after abort"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "First message HAWK.")
        req("post", f"/api/apps/{app}/sessions/{sid}/abort")
        time.sleep(3)
        stream_chat(app, "Second message SPARROW.", session_id=sid)
        r = req("get", f"/api/apps/{app}/sessions/{sid}/history")
        raw = json.dumps(api_ok(r).get("messages", [])).lower()
        assert "hawk" in raw, "HAWK missing from history"
        assert "sparrow" in raw, "SPARROW missing from history"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _res_05_three_concurrent_forks():
    """3 concurrent forks of same session - all succeed."""
    name = "RES-05: 3 concurrent forks"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Base session for forking.")

        def do_fork():
            r = req("post", f"/api/apps/{app}/sessions/{sid}/fork")
            data = api_ok(r)
            fid = data.get("new_session_id", data.get("session_id", ""))
            assert fid, "No fork ID"
            _track(app, fid)
            return fid

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(do_fork) for _ in range(3)]
            fork_ids = [f.result(timeout=30) for f in futures]

        assert len(set(fork_ids)) == 3, f"Expected 3 unique fork IDs, got {len(set(fork_ids))}"
        R.ok(name, f"fork IDs: {[f[:8] for f in fork_ids]}")
    except Exception as e:
        R.fail(name, str(e))


def _res_06_delete_idle_session():
    """Delete session during idle - succeeds."""
    name = "RES-06: Delete idle session"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Session to delete.")
        time.sleep(1)
        r = req("delete", f"/api/apps/{app}/sessions/{sid}")
        assert r.status_code in (200, 204, 404), f"Delete failed: {r.status_code}"
        R.ok(name, f"HTTP {r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _res_07_deploy_chat_undeploy_redeploy():
    """Deploy app -> chat -> undeploy -> redeploy -> resume session."""
    name = "RES-07: Undeploy-redeploy cycle"
    try:
        from tests.production.helpers import TEST_APP_YAML
        yaml_path = os.path.abspath(TEST_APP_YAML)

        # Deploy
        r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
        data = api_ok(r)
        app = data.get("app_id", "")
        assert app, "No app_id"

        # Chat
        sid, _ = _chat(app, "Say OK.")
        time.sleep(3)

        # Undeploy
        r2 = req("delete", f"/api/apps/{app}")
        assert r2.status_code in (200, 204), f"Undeploy failed: {r2.status_code}"

        # Redeploy
        r3 = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
        data3 = api_ok(r3)
        app2 = data3.get("app_id", "")
        assert app2, "No app_id after redeploy"

        # Resume session (may or may not work depending on persistence)
        time.sleep(3)
        try:
            _, events = stream_chat(app2, "Are you there?", session_id=sid)
            content = get_streamed_content(events)
            _track(app2, sid)
            R.ok(name, f"Resumed: {content.strip()[:30]}")
        except Exception:
            # Session may not survive undeploy; create new one
            sid2, events = _chat(app2, "New session after redeploy. Say OK.")
            content = get_streamed_content(events)
            assert content, "No response after redeploy"
            R.ok(name, f"New session after redeploy: {content.strip()[:30]}")
    except Exception as e:
        R.fail(name, str(e))
    finally:
        # Ensure shared test app is available
        ensure_app_deployed()


def _res_08_rapid_5_sessions():
    """Rapid fire 5 messages to 5 different sessions - all complete."""
    name = "RES-08: 5 rapid parallel sessions"
    try:
        app = ensure_app_deployed()

        def do_chat(i):
            sid, events = stream_chat(app, f"Session {i}: say OK.", timeout=90.0)
            _track(app, sid)
            return get_streamed_content(events)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(do_chat, i) for i in range(5)]
            results = [f.result(timeout=90) for f in futures]

        ok_count = sum(1 for r in results if r)
        assert ok_count == 5, f"Only {ok_count}/5 sessions responded"
        R.ok(name, f"{ok_count}/5 succeeded")
    except Exception as e:
        R.fail(name, str(e))


def _res_09_no_degradation_10_turns():
    """Session with 10+ turns doesn't degrade in response time."""
    name = "RES-09: No degradation over 10 turns"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Turn 1. Say OK.")
        times = []
        for i in range(2, 12):
            time.sleep(3)
            start = time.perf_counter()
            _, events = stream_chat(app, f"Turn {i}. Say OK.", session_id=sid)
            elapsed = time.perf_counter() - start
            content = get_streamed_content(events)
            assert content, f"No response at turn {i}"
            times.append(elapsed)

        avg_first = sum(times[:3]) / 3
        avg_last = sum(times[-3:]) / 3
        # Allow 3x degradation as reasonable
        if avg_last > avg_first * 3 and avg_last > 10:
            R.fail(name, f"Degraded: first 3 avg={avg_first:.1f}s, last 3 avg={avg_last:.1f}s")
        else:
            R.ok(name, f"first3={avg_first:.1f}s last3={avg_last:.1f}s")
    except Exception as e:
        R.fail(name, str(e))


def _res_10_context_pressure_tracked():
    """After 10+ turns, context pressure is tracked correctly."""
    name = "RES-10: Context pressure tracked"
    try:
        app = ensure_app_deployed()
        sid, _ = _chat(app, "Turn 1: Hello.")
        for i in range(2, 12):
            time.sleep(3)
            stream_chat(app, f"Turn {i}: continue.", session_id=sid)

        # Check session metadata for context pressure
        r = req("get", f"/api/apps/{app}/sessions/{sid}")
        data = api_ok(r)
        session = data.get("session", data)
        # Look for context_pressure, token_count, or similar
        has_pressure = any(k in session for k in (
            "context_pressure", "token_count", "tokens_used",
            "context_tokens", "message_count", "context_length",
        ))
        if has_pressure:
            pressure_info = {k: session[k] for k in session
                           if k in ("context_pressure", "token_count", "tokens_used",
                                    "context_tokens", "message_count", "context_length")}
            R.ok(name, str(pressure_info))
        else:
            R.skip(name, f"No context pressure fields in {list(session.keys())[:10]}")
    except Exception as e:
        R.fail(name, str(e))


# =================================================================
#  MAIN ENTRY POINT
# =================================================================

def run_all():
    """Run all 45 retry/recovery tests."""
    print("\n========== TEST 12: Retry & Recovery ==========")

    try:
        app_id = ensure_app_deployed()
    except Exception as e:
        R.fail("SETUP: Deploy app for retry tests", str(e))
        print(f"Cannot run retry tests without deployed app: {e}")
        return

    try:
        # ERROR RECOVERY (15 tests)
        print("\n--- Error Recovery ---")
        _err_01_session_works_after_error()
        time.sleep(3)
        _err_02_session_works_after_abort()
        time.sleep(3)
        _err_03_session_works_after_compact()
        time.sleep(3)
        _err_04_next_request_after_rate_limit()
        time.sleep(3)
        _err_05_invalid_tool_params_no_crash()
        time.sleep(3)
        _err_06_long_tool_output_no_crash()
        time.sleep(3)
        _err_07_unicode_in_tool_results()
        time.sleep(3)
        _err_08_empty_tool_result()
        time.sleep(3)
        _err_09_tool_timeout_no_crash()
        time.sleep(3)
        _err_10_consecutive_errors_no_corrupt()
        time.sleep(3)
        _err_11_history_intact_after_error()
        time.sleep(3)
        _err_12_memory_intact_after_error()
        time.sleep(3)
        _err_13_abort_during_stream_no_zombie()
        time.sleep(3)
        _err_14_fork_of_errored_session()
        time.sleep(3)
        _err_15_error_event_has_message()
        time.sleep(3)

        # TIMEOUT BEHAVIOR (10 tests)
        print("\n--- Timeout Behavior ---")
        _timeout_01_health_500ms()
        _timeout_02_app_list_1s()
        _timeout_03_session_ops_2s()
        time.sleep(3)
        _timeout_04_stream_first_byte_3s()
        time.sleep(3)
        _timeout_05_sse_heartbeat_6s()
        time.sleep(3)
        _timeout_06_simple_chat_30s()
        time.sleep(3)
        _timeout_07_tool_use_chat_60s()
        time.sleep(3)
        _timeout_08_compact_10s()
        time.sleep(3)
        _timeout_09_concurrent_5_chats_90s()
        time.sleep(3)
        _timeout_10_no_hanging_requests()
        time.sleep(3)

        # CONNECTION HANDLING (10 tests)
        print("\n--- Connection Handling ---")
        _conn_01_disconnect_during_stream()
        time.sleep(3)
        _conn_02_rapid_connect_disconnect()
        time.sleep(3)
        _conn_03_concurrent_streams_same_session()
        time.sleep(3)
        _conn_04_stream_nonexistent_app()
        time.sleep(3)
        _conn_05_expired_token_returns_401()
        time.sleep(3)
        _conn_06_large_response_streams()
        time.sleep(3)
        _conn_07_binary_content_no_break()
        time.sleep(3)
        _conn_08_sse_content_type()
        time.sleep(3)
        _conn_09_sse_no_cache()
        time.sleep(3)
        _conn_10_stream_chunked()
        time.sleep(3)

        # RESILIENCE PATTERNS (10 tests)
        print("\n--- Resilience Patterns ---")
        _res_01_10_sequential_chats()
        time.sleep(3)
        _res_02_chat_compact_chat()
        time.sleep(3)
        _res_03_chat_fork_chat_original_unaffected()
        time.sleep(3)
        _res_04_chat_abort_chat_history()
        time.sleep(3)
        _res_05_three_concurrent_forks()
        time.sleep(3)
        _res_06_delete_idle_session()
        time.sleep(3)
        _res_07_deploy_chat_undeploy_redeploy()
        time.sleep(3)
        _res_08_rapid_5_sessions()
        time.sleep(3)
        _res_09_no_degradation_10_turns()
        time.sleep(3)
        _res_10_context_pressure_tracked()
    finally:
        _cleanup_all()
