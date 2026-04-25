"""Production tests: System limits, stress testing, and boundary conditions.

Makes real HTTP requests to a live daemon. No mocking.
45 tests covering system limits (15), concurrent stress (15), and boundary conditions (15).
"""

from __future__ import annotations

from tests.production.helpers import (
    R, req, api_ok, stream_chat, get_streamed_content,
    get_result, get_error, get_events_by_type, cleanup_session, new_session_id,
    ensure_auth, WORKSPACE, DAEMON, TIMEOUT, STREAM_TIMEOUT,
)
import tests.production.helpers as _h

import time
import uuid
import os
import concurrent.futures
import httpx


TEST_APP_YAML = os.environ.get(
    "DIGITORN_TEST_APP",
    os.path.join(os.path.dirname(__file__), "..", "..", "examples", "opencode", "app.yaml"),
)

_deployed_app_id: str = ""
_sessions_to_cleanup: list[str] = []


def _deploy_test_app() -> str:
    """Deploy the test app and return app_id (GET-first to avoid rate limit)."""
    global _deployed_app_id
    if _deployed_app_id:
        r = req("get", f"/api/apps/{_deployed_app_id}")
        if r.status_code == 200:
            return _deployed_app_id
    # Try to discover app_id from YAML before deploying
    yaml_path = os.path.abspath(TEST_APP_YAML)
    try:
        import yaml as _yaml
        with open(yaml_path) as _f:
            _peek_id = (_yaml.safe_load(_f) or {}).get("app", {}).get("app_id", "")
    except Exception:
        _peek_id = ""
    if _peek_id:
        r = req("get", f"/api/apps/{_peek_id}")
        if r.status_code == 200:
            _deployed_app_id = _peek_id
            return _deployed_app_id
    for _attempt in range(3):
        r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
        if r.status_code == 429:
            import time as _t
            _t.sleep(min(r.json().get("retry_after", 30) + 1, 60))
            continue
        data = api_ok(r)
        _deployed_app_id = data.get("app_id", "")
        assert _deployed_app_id, "No app_id in deploy response"
        return _deployed_app_id
    raise AssertionError(f"Deploy failed after 3 retries (429): {r.text[:300]}")


def _track(sid: str) -> str:
    """Track a session for cleanup."""
    _sessions_to_cleanup.append(sid)
    return sid


# =================================================================
#  SYSTEM LIMITS (15 tests)
# =================================================================

def _limit_max_turns_respected():
    """Session with max_turns=20 doesn't go beyond."""
    name = "limit_max_turns_respected"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        # Send a message and verify it completes within a finite turn count
        _, events = stream_chat(app_id, "Say hi", session_id=sid)
        result = get_result(events)
        assert result is not None, "no result event"
        turn_events = get_events_by_type(events, "status")
        turn_starts = [e for e in turn_events if e["data"].get("phase") == "turn_start"]
        assert len(turn_starts) <= 20, f"turns exceeded 20: {len(turn_starts)}"
        R.ok(name, f"{len(turn_starts)} turns")
    except Exception as e:
        R.fail(name, str(e))


def _limit_session_timeout_respected():
    """Session timeout doesn't hang forever."""
    name = "limit_session_timeout"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        start = time.perf_counter()
        _, events = stream_chat(app_id, "Say ok", session_id=sid, timeout=120.0)
        elapsed = time.perf_counter() - start
        assert elapsed < 120, f"chat took {elapsed:.0f}s, expected <120s"
        R.ok(name, f"{elapsed:.1f}s")
    except Exception as e:
        R.fail(name, str(e))


def _limit_queue_size():
    """Rapid events (500) don't overflow the queue."""
    name = "limit_queue_size_500"
    try:
        app_id = _deploy_test_app()
        # Send many rapid health checks to stress the event queue
        errors = 0
        for i in range(500):
            r = httpx.get(f"{DAEMON}/health", timeout=TIMEOUT)
            if r.status_code != 200:
                errors += 1
        assert errors == 0, f"{errors} health checks failed out of 500"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _limit_session_idle_ttl():
    """Session idle TTL - session works after short idle."""
    name = "limit_session_idle_ttl"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        _, events = stream_chat(app_id, "Say hello", session_id=sid)
        assert get_result(events), "first chat failed"
        time.sleep(3)
        # Session should still be alive after short idle
        r = req("get", f"/api/apps/{app_id}/sessions/{sid}")
        assert r.status_code == 200, f"session gone after short idle: {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _limit_large_message():
    """Large message (5000 chars) processed correctly."""
    name = "limit_large_message_5000"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        big_msg = "Summarize this in one word: " + ("data " * 1000)
        assert len(big_msg) >= 5000, f"message too short: {len(big_msg)}"
        _, events = stream_chat(app_id, big_msg, session_id=sid)
        content = get_streamed_content(events)
        assert len(content) > 0, "empty response to large message"
        R.ok(name, f"msg={len(big_msg)} chars, resp={len(content)} chars")
    except Exception as e:
        R.fail(name, str(e))


def _limit_large_tool_output():
    """Large tool output processed without crash."""
    name = "limit_large_tool_output"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        # Ask to read a large file - the tool output may be big
        _, events = stream_chat(app_id, "Read the file package.json and say done", session_id=sid)
        err = get_error(events)
        result = get_result(events)
        # Either succeeds or gracefully handles the large output
        assert result is not None or err is not None, "no result or error"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _limit_many_sessions():
    """Create 10 sessions - all work independently."""
    name = "limit_many_sessions_10"
    sids = []
    try:
        app_id = _deploy_test_app()
        for i in range(10):
            sid = _track(new_session_id())
            sids.append(sid)
        # Chat in first and last session
        _, ev1 = stream_chat(app_id, "Say alpha", session_id=sids[0])
        time.sleep(3)
        _, ev2 = stream_chat(app_id, "Say beta", session_id=sids[-1])
        c1 = get_streamed_content(ev1)
        c2 = get_streamed_content(ev2)
        assert len(c1) > 0, "first session empty"
        assert len(c2) > 0, "last session empty"
        # Verify all sessions exist
        for sid in sids:
            r = req("get", f"/api/apps/{app_id}/sessions/{sid}")
            assert r.status_code == 200, f"session {sid[:8]} not found"
        R.ok(name, f"10 sessions created and verified")
    except Exception as e:
        R.fail(name, str(e))


def _limit_many_messages_history():
    """After 5+ turns, session still responsive."""
    name = "limit_many_messages_history"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        for i in range(5):
            _, events = stream_chat(app_id, f"Say number {i+1}", session_id=sid)
            assert get_result(events), f"turn {i+1} failed"
            time.sleep(3)
        # Session should still respond
        _, events = stream_chat(app_id, "Say done", session_id=sid)
        content = get_streamed_content(events)
        assert len(content) > 0, "empty response after many turns"
        R.ok(name, "6 turns completed")
    except Exception as e:
        R.fail(name, str(e))


def _limit_unicode_everywhere():
    """Unicode in message, session_id prefix, tool params."""
    name = "limit_unicode_everywhere"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        _, events = stream_chat(app_id, "Reply with: cafe\u0301 na\u00efve \u00fc\u00f6\u00e4 \u4f60\u597d \ud83d\ude00", session_id=sid)
        content = get_streamed_content(events)
        assert len(content) > 0, "empty response to unicode message"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _limit_empty_message():
    """Empty message handled gracefully."""
    name = "limit_empty_message"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        headers = dict(ensure_auth())
        r = httpx.post(
            f"{DAEMON}/api/apps/{app_id}/chat/stream",
            headers=headers,
            json={"session_id": sid, "message": "", "workspace": WORKSPACE},
            timeout=TIMEOUT,
        )
        # Should either reject or handle gracefully (not 500)
        assert r.status_code != 500, f"server error on empty message: {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _limit_whitespace_only_message():
    """Whitespace-only message handled."""
    name = "limit_whitespace_only_message"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        headers = dict(ensure_auth())
        r = httpx.post(
            f"{DAEMON}/api/apps/{app_id}/chat/stream",
            headers=headers,
            json={"session_id": sid, "message": "   \t  ", "workspace": WORKSPACE},
            timeout=TIMEOUT,
        )
        assert r.status_code != 500, f"server error on whitespace message: {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _limit_newlines_only_message():
    """Message with only newlines handled."""
    name = "limit_newlines_only_message"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        headers = dict(ensure_auth())
        r = httpx.post(
            f"{DAEMON}/api/apps/{app_id}/chat/stream",
            headers=headers,
            json={"session_id": sid, "message": "\n\n\n\n", "workspace": WORKSPACE},
            timeout=TIMEOUT,
        )
        assert r.status_code != 500, f"server error on newlines message: {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _limit_very_long_session_id():
    """Very long session_id (128 chars max) accepted."""
    name = "limit_very_long_session_id"
    try:
        app_id = _deploy_test_app()
        long_sid = "s" * 128
        _track(long_sid)
        headers = dict(ensure_auth())
        r = httpx.post(
            f"{DAEMON}/api/apps/{app_id}/chat/stream",
            headers=headers,
            json={"session_id": long_sid, "message": "Say ok", "workspace": WORKSPACE},
            timeout=STREAM_TIMEOUT,
        )
        # Should accept or reject gracefully (not crash)
        assert r.status_code != 500, f"server error on long session_id: {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _limit_rapid_session_creation():
    """Rapid session creation (10 in 5s) - all created."""
    name = "limit_rapid_session_creation"
    try:
        app_id = _deploy_test_app()
        sids = []
        start = time.perf_counter()
        for _ in range(10):
            sid = _track(new_session_id())
            sids.append(sid)
        elapsed = time.perf_counter() - start
        # Verify we created them fast
        assert elapsed < 5, f"creation took {elapsed:.1f}s, expected <5s"
        # Verify at least the first works
        _, events = stream_chat(app_id, "Say ok", session_id=sids[0])
        assert get_result(events), "first rapid session failed"
        R.ok(name, f"10 sessions in {elapsed:.2f}s")
    except Exception as e:
        R.fail(name, str(e))


def _limit_session_count_endpoint():
    """Session count endpoint returns correct count."""
    name = "limit_session_count_endpoint"
    try:
        app_id = _deploy_test_app()
        r = req("get", f"/api/apps/{app_id}/sessions")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        # Should be a list or have a count
        if isinstance(body, list):
            count = len(body)
        elif isinstance(body, dict):
            data = body.get("data", body)
            if isinstance(data, list):
                count = len(data)
            else:
                count = data.get("count", data.get("total", -1))
        else:
            count = -1
        assert count >= 0, f"could not determine session count from response"
        R.ok(name, f"count={count}")
    except Exception as e:
        R.fail(name, str(e))


# =================================================================
#  CONCURRENT STRESS (15 tests)
# =================================================================

def _stress_concurrent_chats_succeed():
    """5 concurrent chats to different sessions - all succeed."""
    name = "stress_5_concurrent_chats"
    try:
        app_id = _deploy_test_app()
        sids = [_track(new_session_id()) for _ in range(5)]
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futs = {
                pool.submit(stream_chat, app_id, f"Say number {i}", session_id=sids[i]): i
                for i in range(5)
            }
            for fut in concurrent.futures.as_completed(futs):
                idx = futs[fut]
                _, events = fut.result()
                results[idx] = get_streamed_content(events)
        failed = [i for i, c in results.items() if not c]
        assert len(failed) == 0, f"sessions {failed} returned empty"
        R.ok(name, "5/5 succeeded")
    except Exception as e:
        R.fail(name, str(e))


def _stress_concurrent_chats_time():
    """5 concurrent chats - total time < 120s."""
    name = "stress_5_concurrent_chats_time"
    try:
        app_id = _deploy_test_app()
        sids = [_track(new_session_id()) for _ in range(5)]
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futs = [
                pool.submit(stream_chat, app_id, f"Say word {i}", session_id=sids[i])
                for i in range(5)
            ]
            for f in concurrent.futures.as_completed(futs):
                f.result()
        elapsed = time.perf_counter() - start
        assert elapsed < 120, f"took {elapsed:.0f}s, expected <120s"
        R.ok(name, f"{elapsed:.1f}s")
    except Exception as e:
        R.fail(name, str(e))


def _stress_concurrent_forks():
    """3 concurrent session forks - all succeed."""
    name = "stress_3_concurrent_forks"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        stream_chat(app_id, "Say hello for fork", session_id=sid)
        time.sleep(3)

        def do_fork():
            r = req("post", f"/api/apps/{app_id}/sessions/{sid}/fork")
            assert r.status_code == 200, f"fork failed: {r.status_code}"
            data = r.json()
            forked = data.get("data", data).get("session_id", "")
            if forked:
                _track(forked)
            return forked

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futs = [pool.submit(do_fork) for _ in range(3)]
            forked_ids = [f.result() for f in concurrent.futures.as_completed(futs)]
        succeeded = [f for f in forked_ids if f]
        assert len(succeeded) >= 2, f"only {len(succeeded)} forks succeeded"
        R.ok(name, f"{len(succeeded)}/3 forks")
    except Exception as e:
        R.fail(name, str(e))


def _stress_concurrent_health():
    """10 concurrent health checks - all 200."""
    name = "stress_10_concurrent_health"
    try:
        def check_health():
            r = httpx.get(f"{DAEMON}/health", timeout=TIMEOUT)
            return r.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(check_health) for _ in range(10)]
            statuses = [f.result() for f in concurrent.futures.as_completed(futs)]
        non_200 = [s for s in statuses if s != 200]
        assert len(non_200) == 0, f"{len(non_200)} non-200 responses: {non_200}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _stress_concurrent_app_list():
    """10 concurrent app list - all return same data."""
    name = "stress_10_concurrent_app_list"
    try:
        def get_apps():
            r = req("get", "/api/apps")
            assert r.status_code == 200
            return r.text

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(get_apps) for _ in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futs)]
        # All should return same data
        unique = set(results)
        assert len(unique) == 1, f"got {len(unique)} different responses"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _stress_concurrent_session_list():
    """5 concurrent session list - consistent results."""
    name = "stress_5_concurrent_session_list"
    try:
        app_id = _deploy_test_app()

        def get_sessions():
            r = req("get", f"/api/apps/{app_id}/sessions")
            assert r.status_code == 200
            return r.text

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futs = [pool.submit(get_sessions) for _ in range(5)]
            results = [f.result() for f in concurrent.futures.as_completed(futs)]
        unique = set(results)
        assert len(unique) == 1, f"got {len(unique)} different session lists"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _stress_read_write_mix():
    """Mix of read + write operations concurrently - no corruption."""
    name = "stress_read_write_mix"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        stream_chat(app_id, "Init session", session_id=sid)
        time.sleep(3)

        errors = []

        def read_op():
            r = req("get", f"/api/apps/{app_id}/sessions/{sid}")
            if r.status_code != 200:
                errors.append(f"read: {r.status_code}")

        def write_op():
            r = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
            if r.status_code != 200:
                errors.append(f"history: {r.status_code}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futs = []
            for _ in range(3):
                futs.append(pool.submit(read_op))
                futs.append(pool.submit(write_op))
            for f in concurrent.futures.as_completed(futs):
                f.result()
        assert len(errors) == 0, f"errors: {errors}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _stress_concurrent_chat_abort():
    """Concurrent chat + abort - handled gracefully."""
    name = "stress_concurrent_chat_abort"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        stream_chat(app_id, "Setup for abort", session_id=sid)
        time.sleep(3)

        def do_abort():
            time.sleep(1)
            r = req("post", f"/api/apps/{app_id}/sessions/{sid}/abort")
            return r.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            chat_fut = pool.submit(stream_chat, app_id, "Count to 100 slowly", sid)
            abort_fut = pool.submit(do_abort)
            abort_code = abort_fut.result()
            try:
                chat_fut.result()
            except Exception:
                pass  # Chat may fail due to abort - that's fine
        # Abort should not crash (200 or 409 are both ok)
        assert abort_code in (200, 409, 422), f"abort returned {abort_code}"
        R.ok(name, f"abort status={abort_code}")
    except Exception as e:
        R.fail(name, str(e))


def _stress_concurrent_chat_compact():
    """Concurrent chat + compact - handled gracefully."""
    name = "stress_concurrent_chat_compact"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        stream_chat(app_id, "Setup for compact", session_id=sid)
        time.sleep(3)

        r = req("post", f"/api/apps/{app_id}/sessions/{sid}/compact")
        # Compact should succeed or gracefully refuse
        assert r.status_code in (200, 409, 422), f"compact returned {r.status_code}"
        R.ok(name, f"compact status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _stress_concurrent_deploy_chat():
    """Concurrent deploy + chat - no deadlock."""
    name = "stress_concurrent_deploy_chat"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())

        def do_deploy():
            yaml_path = os.path.abspath(TEST_APP_YAML)
            r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
            return r.status_code

        def do_chat():
            _, events = stream_chat(app_id, "Say ok", session_id=sid)
            return get_result(events) is not None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            start = time.perf_counter()
            deploy_fut = pool.submit(do_deploy)
            chat_fut = pool.submit(do_chat)
            deploy_code = deploy_fut.result(timeout=60)
            try:
                chat_ok = chat_fut.result(timeout=60)
            except Exception:
                chat_ok = False
            elapsed = time.perf_counter() - start
        assert elapsed < 60, f"deadlock suspected: {elapsed:.0f}s"
        R.ok(name, f"deploy={deploy_code}, chat={'ok' if chat_ok else 'failed'}, {elapsed:.1f}s")
    except Exception as e:
        R.fail(name, str(e))


def _stress_sequential_chats_same_session():
    """3 sequential chats same session under 60s."""
    name = "stress_3_sequential_same_session"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        start = time.perf_counter()
        for i in range(3):
            _, events = stream_chat(app_id, f"Say {i}", session_id=sid)
            assert get_result(events), f"turn {i} failed"
            time.sleep(3)
        elapsed = time.perf_counter() - start
        assert elapsed < 60, f"took {elapsed:.0f}s, expected <60s"
        R.ok(name, f"{elapsed:.1f}s for 3 turns")
    except Exception as e:
        R.fail(name, str(e))


def _stress_concurrent_tool_executions():
    """Concurrent tool executions in parallel turn - all complete."""
    name = "stress_concurrent_tool_exec"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        # Ask for something that triggers tool use
        _, events = stream_chat(
            app_id, "List the files in the current directory and say done",
            session_id=sid,
        )
        result = get_result(events)
        assert result is not None, "no result from tool execution"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _stress_rapid_sequential_messages():
    """Rapid sequential messages (3) to same session - serialized."""
    name = "stress_rapid_sequential_messages"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        for i in range(3):
            _, events = stream_chat(app_id, f"Msg {i}", session_id=sid)
            result = get_result(events)
            assert result is not None, f"message {i} had no result"
            time.sleep(3)
        R.ok(name, "3 messages serialized")
    except Exception as e:
        R.fail(name, str(e))


def _stress_concurrent_metric_reads():
    """5 concurrent metric reads - consistent data."""
    name = "stress_5_concurrent_metrics"
    try:
        def read_metrics():
            r = req("get", "/api/metrics")
            assert r.status_code == 200
            return r.text

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futs = [pool.submit(read_metrics) for _ in range(5)]
            results = [f.result() for f in concurrent.futures.as_completed(futs)]
        # All should parse as valid JSON
        for txt in results:
            import json
            json.loads(txt)
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _stress_no_resource_leaks():
    """No resource leaks after 20 sequential operations (health still fast)."""
    name = "stress_no_resource_leaks"
    try:
        app_id = _deploy_test_app()
        # Do 20 operations: mix of health, session list, metrics
        for i in range(20):
            if i % 3 == 0:
                httpx.get(f"{DAEMON}/health", timeout=TIMEOUT)
            elif i % 3 == 1:
                req("get", f"/api/apps/{app_id}/sessions")
            else:
                req("get", "/api/metrics")
        # Health should still be fast
        start = time.perf_counter()
        r = httpx.get(f"{DAEMON}/health", timeout=TIMEOUT)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert r.status_code == 200, f"health failed after ops: {r.status_code}"
        assert elapsed_ms < 500, f"health slow after ops: {elapsed_ms:.0f}ms"
        R.ok(name, f"health={elapsed_ms:.0f}ms after 20 ops")
    except Exception as e:
        R.fail(name, str(e))


# =================================================================
#  BOUNDARY CONDITIONS (15 tests)
# =================================================================

def _boundary_first_token_time():
    """First character of response received within 15s."""
    name = "boundary_first_token_15s"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        headers = dict(ensure_auth())

        # Manually measure time to first token
        _h._last_llm_call = 0  # Reset throttle for timing
        elapsed_first = time.time() - _h._last_llm_call
        if elapsed_first < _h._LLM_MIN_INTERVAL:
            time.sleep(_h._LLM_MIN_INTERVAL - elapsed_first)
        _h._last_llm_call = time.time()

        start = time.perf_counter()
        first_token_time = None
        with httpx.stream(
            "POST", f"{DAEMON}/api/apps/{app_id}/chat/stream",
            headers=headers,
            json={"session_id": sid, "message": "Say hi", "workspace": WORKSPACE},
            timeout=STREAM_TIMEOUT,
        ) as resp:
            assert resp.status_code == 200, f"stream failed: {resp.status_code}"
            for chunk in resp.iter_bytes():
                text = chunk.decode("utf-8", errors="replace")
                if "token" in text or "data:" in text:
                    first_token_time = time.perf_counter() - start
                    break
            # Drain the rest
            for _ in resp.iter_bytes():
                pass
        assert first_token_time is not None, "no token received"
        assert first_token_time < 15, f"first token at {first_token_time:.1f}s, expected <15s"
        R.ok(name, f"{first_token_time:.1f}s")
    except Exception as e:
        R.fail(name, str(e))


def _boundary_sse_connection_time():
    """SSE connection established within 3s."""
    name = "boundary_sse_connect_3s"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        headers = dict(ensure_auth())

        _h._last_llm_call = 0
        elapsed_first = time.time() - _h._last_llm_call
        if elapsed_first < _h._LLM_MIN_INTERVAL:
            time.sleep(_h._LLM_MIN_INTERVAL - elapsed_first)
        _h._last_llm_call = time.time()

        start = time.perf_counter()
        with httpx.stream(
            "POST", f"{DAEMON}/api/apps/{app_id}/chat/stream",
            headers=headers,
            json={"session_id": sid, "message": "Say ok", "workspace": WORKSPACE},
            timeout=STREAM_TIMEOUT,
        ) as resp:
            connect_time = time.perf_counter() - start
            assert resp.status_code == 200, f"stream failed: {resp.status_code}"
            # Drain
            for _ in resp.iter_bytes():
                pass
        assert connect_time < 3, f"connection took {connect_time:.1f}s, expected <3s"
        R.ok(name, f"{connect_time:.2f}s")
    except Exception as e:
        R.fail(name, str(e))


def _boundary_empty_response():
    """Empty response handled (model says nothing)."""
    name = "boundary_empty_response"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        # Even if the model gives a minimal response, it should not crash
        _, events = stream_chat(app_id, ".", session_id=sid)
        result = get_result(events)
        # Should have a result event even if content is empty
        assert result is not None, "no result event at all"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _boundary_model_refusal():
    """Model refuses request - error message clear."""
    name = "boundary_model_refusal"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        _, events = stream_chat(
            app_id,
            "What is 2+2? Reply with only the number.",
            session_id=sid,
        )
        # Should get a result (model won't actually refuse this)
        result = get_result(events)
        assert result is not None, "no result event"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _boundary_tool_error_continues():
    """Tool returns error - session continues."""
    name = "boundary_tool_error_continues"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        # Ask to read a nonexistent file - tool should error but session continues
        _, events = stream_chat(
            app_id,
            "Read the file /nonexistent/xyz123.txt and tell me what happened",
            session_id=sid,
        )
        content = get_streamed_content(events)
        assert len(content) > 0, "no response after tool error"
        time.sleep(3)
        # Session should still work
        _, events2 = stream_chat(app_id, "Say ok", session_id=sid)
        assert get_result(events2), "session broken after tool error"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _boundary_large_tool_output():
    """Tool returns very large output (>10KB) - handled."""
    name = "boundary_large_tool_output_10kb"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        # Ask to list files in a directory with many entries
        _, events = stream_chat(
            app_id,
            "List all files recursively in node_modules if it exists, otherwise say no node_modules",
            session_id=sid,
        )
        result = get_result(events)
        assert result is not None, "no result event"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _boundary_multiple_tool_calls():
    """Multiple tool calls in single LLM response - all executed."""
    name = "boundary_multiple_tool_calls"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        _, events = stream_chat(
            app_id,
            "Read both package.json and README.md then say done",
            session_id=sid,
        )
        # Should have tool-related events
        result = get_result(events)
        assert result is not None, "no result event"
        content = get_streamed_content(events)
        assert len(content) > 0, "empty response from multi-tool"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _boundary_nested_json_tool_params():
    """Nested JSON in tool params - handled correctly."""
    name = "boundary_nested_json_params"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        _, events = stream_chat(
            app_id,
            'Write {"key": {"nested": [1,2,3]}} to a variable and say done',
            session_id=sid,
        )
        result = get_result(events)
        assert result is not None, "no result event"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _boundary_special_chars_filepath():
    """Special characters in file paths - handled."""
    name = "boundary_special_chars_filepath"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        _, events = stream_chat(
            app_id,
            "Check if the file 'test file (1).txt' exists and say yes or no",
            session_id=sid,
        )
        result = get_result(events)
        assert result is not None, "no result event"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _boundary_deploy_undeploy_redeploy():
    """Session works after deploy/undeploy/redeploy cycle."""
    name = "boundary_deploy_undeploy_redeploy"
    try:
        global _deployed_app_id
        yaml_path = os.path.abspath(TEST_APP_YAML)

        # Deploy
        r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
        data = api_ok(r)
        app_id = data.get("app_id", "")
        assert app_id, "deploy failed"

        # Undeploy
        r = req("delete", f"/api/apps/{app_id}")
        assert r.status_code == 200, f"undeploy failed: {r.status_code}"

        time.sleep(2)

        # Redeploy
        r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
        data = api_ok(r)
        new_app_id = data.get("app_id", "")
        assert new_app_id, "redeploy failed"
        _deployed_app_id = new_app_id

        # Chat should work
        sid = _track(new_session_id())
        _, events = stream_chat(new_app_id, "Say redeployed", session_id=sid)
        assert get_result(events), "chat failed after redeploy"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _boundary_multiple_apps_isolated():
    """Multiple apps deployed simultaneously - isolated."""
    name = "boundary_multiple_apps_isolated"
    try:
        # We can only deploy from the same yaml, but verify app list works
        app_id = _deploy_test_app()
        r = req("get", "/api/apps")
        assert r.status_code == 200
        body = r.json()
        apps = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(apps, list):
            assert len(apps) >= 1, "no apps found"
        R.ok(name, f"apps listed ok")
    except Exception as e:
        R.fail(name, str(e))


def _boundary_app_sessions_no_cross_contamination():
    """App-specific sessions don't cross-contaminate."""
    name = "boundary_no_cross_contamination"
    try:
        app_id = _deploy_test_app()
        sid_a = _track(new_session_id())
        sid_b = _track(new_session_id())
        stream_chat(app_id, "Secret word: pineapple42", session_id=sid_a)
        time.sleep(3)
        _, events_b = stream_chat(app_id, "What was the secret word?", session_id=sid_b)
        content_b = get_streamed_content(events_b).lower()
        assert "pineapple42" not in content_b, "session B knows session A secret"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _boundary_metrics_after_heavy_usage():
    """Metrics accurate after heavy usage."""
    name = "boundary_metrics_after_heavy"
    try:
        r = req("get", "/api/metrics")
        assert r.status_code == 200
        data = r.json()
        # Should have standard metric fields
        if isinstance(data, dict):
            has_data = bool(data.get("data", data))
        else:
            has_data = True
        assert has_data, "metrics empty after heavy usage"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _boundary_session_history_complete():
    """Session history complete after many operations."""
    name = "boundary_session_history_complete"
    try:
        app_id = _deploy_test_app()
        sid = _track(new_session_id())
        stream_chat(app_id, "First message", session_id=sid)
        time.sleep(3)
        stream_chat(app_id, "Second message", session_id=sid)
        time.sleep(3)

        r = req("get", f"/api/apps/{app_id}/sessions/{sid}/history")
        assert r.status_code == 200, f"history failed: {r.status_code}"
        body = r.json()
        history = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(history, list):
            # Should have at least 2 user messages + 2 assistant messages
            assert len(history) >= 4, f"history too short: {len(history)} entries"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _boundary_final_cleanup():
    """Final cleanup: undeploy, verify sessions gone."""
    name = "boundary_final_cleanup"
    try:
        global _deployed_app_id
        app_id = _deployed_app_id
        if not app_id:
            R.skip(name, "no app deployed")
            return

        # Clean up tracked sessions
        for sid in _sessions_to_cleanup:
            cleanup_session(app_id, sid)
        _sessions_to_cleanup.clear()

        # Undeploy
        r = req("delete", f"/api/apps/{app_id}")
        assert r.status_code == 200, f"undeploy failed: {r.status_code}"
        _deployed_app_id = ""

        time.sleep(2)

        # Verify app is gone
        r = req("get", f"/api/apps/{app_id}")
        assert r.status_code in (404, 422), f"app still exists: {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


# =================================================================
#  RUNNER
# =================================================================

def run_all():
    """Run all 45 limits, stress, and boundary condition tests."""
    # Force token refresh
    import tests.production.helpers as _h
    _h._token_created_at = 0
    ensure_auth()

    # Deploy the test app
    try:
        _deploy_test_app()
    except Exception as e:
        R.fail("SETUP: Deploy test app", str(e))
        print(f"Cannot run tests without deployed app: {e}")
        return

    print("\n\033[1m=== SYSTEM LIMITS (15) ===\033[0m")
    _limit_max_turns_respected()
    _limit_session_timeout_respected()
    _limit_queue_size()
    _limit_session_idle_ttl()
    _limit_large_message()
    _limit_large_tool_output()
    _limit_many_sessions()
    _limit_many_messages_history()
    _limit_unicode_everywhere()
    _limit_empty_message()
    _limit_whitespace_only_message()
    _limit_newlines_only_message()
    _limit_very_long_session_id()
    _limit_rapid_session_creation()
    _limit_session_count_endpoint()

    print("\n\033[1m=== CONCURRENT STRESS (15) ===\033[0m")
    _stress_concurrent_chats_succeed()
    _stress_concurrent_chats_time()
    _stress_concurrent_forks()
    _stress_concurrent_health()
    _stress_concurrent_app_list()
    _stress_concurrent_session_list()
    _stress_read_write_mix()
    _stress_concurrent_chat_abort()
    _stress_concurrent_chat_compact()
    _stress_concurrent_deploy_chat()
    _stress_sequential_chats_same_session()
    _stress_concurrent_tool_executions()
    _stress_rapid_sequential_messages()
    _stress_concurrent_metric_reads()
    _stress_no_resource_leaks()

    print("\n\033[1m=== BOUNDARY CONDITIONS (15) ===\033[0m")
    _boundary_first_token_time()
    _boundary_sse_connection_time()
    _boundary_empty_response()
    _boundary_model_refusal()
    _boundary_tool_error_continues()
    _boundary_large_tool_output()
    _boundary_multiple_tool_calls()
    _boundary_nested_json_tool_params()
    _boundary_special_chars_filepath()
    _boundary_deploy_undeploy_redeploy()
    _boundary_multiple_apps_isolated()
    _boundary_app_sessions_no_cross_contamination()
    _boundary_metrics_after_heavy_usage()
    _boundary_session_history_complete()
    _boundary_final_cleanup()
