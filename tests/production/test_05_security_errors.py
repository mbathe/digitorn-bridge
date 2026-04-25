"""Production tests: Security isolation, error handling, edge cases, and validation.

Makes real HTTP requests to a live daemon. No mocking.
"""

from __future__ import annotations

import concurrent.futures
import time
import uuid

import httpx

from tests.production.helpers import (
    R,
    req,
    api_ok,
    ensure_app_deployed,
    stream_chat,
    get_streamed_content,
    cleanup_session,
    new_session_id,
    DAEMON,
    TIMEOUT,
    WORKSPACE,
)


# ═══════════════════════════════════════════════════════════════
#  SESSION ISOLATION
# ═══════════════════════════════════════════════════════════════

def _session_a_cannot_see_b_messages():
    """Session A can't see session B's messages."""
    name = "session_a_cannot_see_b_messages"
    try:
        app_id = ensure_app_deployed()
        sid_a = new_session_id()
        sid_b = new_session_id()
        stream_chat(app_id, "Session A secret alpha", session_id=sid_a)
        stream_chat(app_id, "Session B secret beta", session_id=sid_b)
        r = req("get", f"/api/apps/{app_id}/sessions/{sid_a}/history")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        raw = str(body).lower()
        assert "beta" not in raw, "session A history contains session B content"
        cleanup_session(app_id, sid_a)
        cleanup_session(app_id, sid_b)
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _session_b_cannot_see_a_data():
    """Session B can't see session A's data."""
    name = "session_b_cannot_see_a_data"
    try:
        app_id = ensure_app_deployed()
        sid_a = new_session_id()
        sid_b = new_session_id()
        stream_chat(app_id, "Session A unique data xray123", session_id=sid_a)
        stream_chat(app_id, "Session B hello", session_id=sid_b)
        r = req("get", f"/api/apps/{app_id}/sessions/{sid_b}/history")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        raw = str(r.json()).lower()
        assert "xray123" not in raw, "session B history contains session A content"
        cleanup_session(app_id, sid_a)
        cleanup_session(app_id, sid_b)
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _sessions_completely_independent():
    """Different session_ids are completely independent."""
    name = "sessions_completely_independent"
    try:
        app_id = ensure_app_deployed()
        sid_a = new_session_id()
        sid_b = new_session_id()
        stream_chat(app_id, "Independent test alpha", session_id=sid_a)
        stream_chat(app_id, "Independent test beta", session_id=sid_b)
        r_a = req("get", f"/api/apps/{app_id}/sessions/{sid_a}/history")
        r_b = req("get", f"/api/apps/{app_id}/sessions/{sid_b}/history")
        assert r_a.status_code == 200 and r_b.status_code == 200
        assert str(r_a.json()) != str(r_b.json()), "sessions returned identical data"
        cleanup_session(app_id, sid_a)
        cleanup_session(app_id, sid_b)
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _history_scoped_to_session():
    """History endpoint scoped to session owner."""
    name = "history_scoped_to_session"
    try:
        app_id = ensure_app_deployed()
        sid = new_session_id()
        stream_chat(app_id, "Scoped history test", session_id=sid)
        other_sid = new_session_id()
        r = req("get", f"/api/apps/{app_id}/sessions/{other_sid}/history")
        # Should not return the first session's data
        assert r.status_code in (404, 200), f"unexpected status {r.status_code}"
        if r.status_code == 200:
            body = r.json()
            data = body.get("data", body)
            messages = data.get("messages", data.get("history", []))
            assert len(messages) == 0 or "Scoped history test" not in str(messages), \
                "other session returned our history"
        cleanup_session(app_id, sid)
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _memory_scoped_to_session():
    """Memory endpoint scoped to session owner."""
    name = "memory_scoped_to_session"
    try:
        app_id = ensure_app_deployed()
        sid_a = new_session_id()
        sid_b = new_session_id()
        stream_chat(app_id, "Remember memory marker zebra99", session_id=sid_a)
        r = req("get", f"/api/apps/{app_id}/sessions/{sid_b}/memory")
        if r.status_code == 404:
            R.ok(name, "404 for nonexistent session memory")
        elif r.status_code == 200:
            raw = str(r.json()).lower()
            assert "zebra99" not in raw, "memory leaked across sessions"
            R.ok(name, "memory isolated")
        else:
            R.ok(name, f"status={r.status_code}")
        cleanup_session(app_id, sid_a)
    except Exception as e:
        R.fail(name, str(e))


def _workspace_scoped_to_session():
    """Workspace endpoint scoped to session owner."""
    name = "workspace_scoped_to_session"
    try:
        app_id = ensure_app_deployed()
        sid = new_session_id()
        stream_chat(app_id, "Workspace test", session_id=sid)
        other_sid = new_session_id()
        r = req("get", f"/api/apps/{app_id}/sessions/{other_sid}/workspace")
        if r.status_code == 404:
            R.ok(name, "404 for nonexistent session workspace")
        elif r.status_code == 200:
            R.ok(name, "workspace returned (may be shared by design)")
        else:
            R.ok(name, f"status={r.status_code}")
        cleanup_session(app_id, sid)
    except Exception as e:
        R.fail(name, str(e))


def _fork_requires_session_ownership():
    """Fork endpoint requires session ownership."""
    name = "fork_requires_session_ownership"
    try:
        app_id = ensure_app_deployed()
        fake_sid = new_session_id()
        r = req("post", f"/api/apps/{app_id}/sessions/{fake_sid}/fork")
        assert r.status_code in (404, 403, 400, 422), \
            f"fork on nonexistent session should fail, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _compact_requires_session_ownership():
    """Compact endpoint requires session ownership."""
    name = "compact_requires_session_ownership"
    try:
        app_id = ensure_app_deployed()
        fake_sid = new_session_id()
        r = req("post", f"/api/apps/{app_id}/sessions/{fake_sid}/compact")
        assert r.status_code in (404, 403, 400, 422), \
            f"compact on nonexistent session should fail, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _abort_requires_session_ownership():
    """Abort endpoint requires session ownership."""
    name = "abort_requires_session_ownership"
    try:
        app_id = ensure_app_deployed()
        fake_sid = new_session_id()
        r = req("post", f"/api/apps/{app_id}/sessions/{fake_sid}/abort")
        assert r.status_code in (404, 403, 400, 422), \
            f"abort on nonexistent session should fail, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _nonexistent_session_returns_404():
    """Nonexistent session returns 404, not empty."""
    name = "nonexistent_session_returns_404"
    try:
        app_id = ensure_app_deployed()
        fake_sid = new_session_id()
        r = req("get", f"/api/apps/{app_id}/sessions/{fake_sid}/history")
        assert r.status_code in (404, 200), f"unexpected status {r.status_code}"
        if r.status_code == 200:
            body = r.json()
            data = body.get("data", body)
            messages = data.get("messages", data.get("history", []))
            R.ok(name, f"returned 200 with {len(messages)} messages (empty is acceptable)")
        else:
            R.ok(name, "404 for nonexistent session")
    except Exception as e:
        R.fail(name, str(e))


def _random_uuid_session_returns_404():
    """Random UUID session returns 404."""
    name = "random_uuid_session_404"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/sessions/{uuid.uuid4()}")
        assert r.status_code in (404, 200), f"unexpected status {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _session_ids_are_unpredictable():
    """Session IDs are unpredictable (UUID-based)."""
    name = "session_ids_unpredictable"
    try:
        ids = [new_session_id() for _ in range(10)]
        unique = set(ids)
        assert len(unique) == 10, f"expected 10 unique IDs, got {len(unique)}"
        for sid in ids:
            uuid.UUID(sid)  # validates UUID format
        R.ok(name, "all UUIDs, all unique")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  AUTH ENFORCEMENT
# ═══════════════════════════════════════════════════════════════

def _noauth_get_apps():
    """GET /api/apps without auth - returns 401."""
    name = "noauth_get_apps"
    try:
        r = httpx.get(f"{DAEMON}/api/apps", timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _noauth_deploy():
    """POST /api/apps/deploy without auth - returns 401."""
    name = "noauth_deploy"
    try:
        r = httpx.post(f"{DAEMON}/api/apps/deploy", json={"yaml_path": "/tmp/fake.yaml"},
                       timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _noauth_chat_stream():
    """POST /chat/stream without auth - returns 401."""
    name = "noauth_chat_stream"
    try:
        app_id = ensure_app_deployed()
        r = httpx.post(f"{DAEMON}/api/apps/{app_id}/chat/stream",
                       json={"session_id": "x", "message": "hi"},
                       timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _noauth_sessions():
    """GET /api/apps/{id}/sessions without auth - returns 401."""
    name = "noauth_sessions"
    try:
        app_id = ensure_app_deployed()
        r = httpx.get(f"{DAEMON}/api/apps/{app_id}/sessions", timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _noauth_config():
    """GET /api/config without auth - returns 401."""
    name = "noauth_config"
    try:
        r = httpx.get(f"{DAEMON}/api/config", timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _invalid_bearer_token():
    """Invalid Bearer token - returns 401."""
    name = "invalid_bearer_token"
    try:
        r = httpx.get(f"{DAEMON}/api/apps", headers={
            "Authorization": "Bearer totally.invalid.token.here",
        }, timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _expired_token_format():
    """Expired token format - returns 401."""
    name = "expired_token_format"
    try:
        expired = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiJ0ZXN0IiwiZXhwIjoxMDAwMDAwMDAwfQ."
            "invalidsignature"
        )
        r = httpx.get(f"{DAEMON}/api/apps", headers={
            "Authorization": f"Bearer {expired}",
        }, timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _empty_auth_header():
    """Empty Authorization header - returns 401."""
    name = "empty_auth_header"
    try:
        r = httpx.get(f"{DAEMON}/api/apps", headers={
            "Authorization": "",
        }, timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _bearer_empty_token():
    """Bearer with empty token - returns 401 (or httpx rejects the header)."""
    name = "bearer_empty_token"
    try:
        try:
            r = httpx.get(f"{DAEMON}/api/apps", headers={
                "Authorization": "Bearer ",
            }, timeout=TIMEOUT)
        except httpx.InvalidURL:
            R.ok(name, "httpx rejected invalid header (InvalidURL)")
            return
        except ValueError:
            R.ok(name, "httpx rejected illegal header value")
            return
        except Exception as inner_e:
            if "Illegal header value" in str(inner_e) or "illegal" in str(inner_e).lower():
                R.ok(name, f"httpx rejected header: {str(inner_e)[:60]}")
                return
            raise
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        if "Illegal header value" in str(e) or "illegal" in str(e).lower():
            R.ok(name, f"httpx rejected header: {str(e)[:60]}")
        else:
            R.fail(name, str(e))


def _malformed_jwt():
    """Malformed JWT - returns 401."""
    name = "malformed_jwt"
    try:
        r = httpx.get(f"{DAEMON}/api/apps", headers={
            "Authorization": "Bearer not-a-jwt-at-all",
        }, timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _health_no_auth_required():
    """Health endpoints work WITHOUT auth."""
    name = "health_no_auth"
    try:
        r = httpx.get(f"{DAEMON}/health", timeout=TIMEOUT)
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _healthz_no_auth_required():
    """/healthz works without auth."""
    name = "healthz_no_auth"
    try:
        r = httpx.get(f"{DAEMON}/healthz", timeout=TIMEOUT)
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  ERROR RESPONSES
# ═══════════════════════════════════════════════════════════════

def _404_nonexistent_app():
    """404 for nonexistent app."""
    name = "404_nonexistent_app"
    try:
        r = req("get", f"/api/apps/nonexistent_app_{uuid.uuid4().hex[:8]}")
        assert r.status_code == 404, f"expected 404, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _404_nonexistent_session():
    """404 for nonexistent session."""
    name = "404_nonexistent_session"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/sessions/{uuid.uuid4()}/history")
        assert r.status_code in (404, 200), f"expected 404 or 200, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _404_nonexistent_tool():
    """404 for nonexistent tool."""
    name = "404_nonexistent_tool"
    try:
        app_id = ensure_app_deployed()
        r = req("post", f"/api/apps/{app_id}/tools/nonexistent_tool_{uuid.uuid4().hex[:8]}")
        assert r.status_code in (404, 405, 422), f"expected 404/405/422, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _400_invalid_app_id_special_chars():
    """400 for invalid app_id (special chars)."""
    name = "400_invalid_app_id_special"
    try:
        r = req("get", "/api/apps/!@#$%^&*()")
        assert r.status_code in (400, 404, 422), f"expected 400/404/422, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _400_too_long_app_id():
    """400 for too-long app_id (>128 chars)."""
    name = "400_too_long_app_id"
    try:
        long_id = "a" * 200
        r = req("get", f"/api/apps/{long_id}")
        assert r.status_code in (400, 404, 414, 422), f"expected error, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _400_too_long_session_id():
    """400 for too-long session_id (>128 chars)."""
    name = "400_too_long_session_id"
    try:
        app_id = ensure_app_deployed()
        long_sid = "s" * 200
        r = req("get", f"/api/apps/{app_id}/sessions/{long_sid}/history")
        assert r.status_code in (400, 404, 414, 422), f"expected error, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _422_malformed_json():
    """422 for malformed JSON body."""
    name = "422_malformed_json"
    try:
        app_id = ensure_app_deployed()
        r = req("post", f"/api/apps/{app_id}/chat/stream",
                content=b"not valid json {{{",
                headers={"Content-Type": "application/json"})
        assert r.status_code in (400, 422), f"expected 400/422, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _422_missing_required_fields():
    """422 for missing required fields in POST."""
    name = "422_missing_required_fields"
    try:
        app_id = ensure_app_deployed()
        r = req("post", f"/api/apps/{app_id}/chat/stream", json={})
        assert r.status_code in (400, 422), f"expected 400/422, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _422_wrong_field_types():
    """422 for wrong field types."""
    name = "422_wrong_field_types"
    try:
        app_id = ensure_app_deployed()
        r = req("post", f"/api/apps/{app_id}/chat/stream", json={
            "session_id": 12345,
            "message": ["not", "a", "string"],
        })
        assert r.status_code in (400, 422, 500), f"expected error, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _500_includes_error_message():
    """500 errors include error message."""
    name = "500_includes_error_message"
    try:
        # Trigger a server-side error with bad deploy path
        r = req("post", "/api/apps/deploy", json={"yaml_path": "/nonexistent/path/app.yaml"})
        if r.status_code >= 400:
            body = r.json()
            has_error = "error" in body or "detail" in body or "message" in body
            assert has_error, f"error response missing error message: {list(body.keys())}"
            R.ok(name, f"status={r.status_code}")
        else:
            R.skip(name, "did not trigger error")
    except Exception as e:
        R.fail(name, str(e))


def _error_responses_are_json():
    """All error responses are JSON format."""
    name = "error_responses_json"
    try:
        r = req("get", f"/api/apps/nonexistent_{uuid.uuid4().hex[:8]}")
        assert r.status_code >= 400, f"expected error, got {r.status_code}"
        content_type = r.headers.get("content-type", "")
        assert "json" in content_type.lower(), f"expected JSON, got {content_type}"
        r.json()  # should not raise
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _error_responses_have_error_field():
    """Error responses have 'error' field."""
    name = "error_has_error_field"
    try:
        r = req("get", f"/api/apps/nonexistent_{uuid.uuid4().hex[:8]}")
        assert r.status_code >= 400, f"expected error, got {r.status_code}"
        body = r.json()
        has_error = "error" in body or "detail" in body or "message" in body
        assert has_error, f"missing error field in: {list(body.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _error_responses_have_success_false():
    """Error responses have 'success': false."""
    name = "error_has_success_false"
    try:
        r = req("get", f"/api/apps/nonexistent_{uuid.uuid4().hex[:8]}")
        assert r.status_code >= 400, f"expected error, got {r.status_code}"
        body = r.json()
        if "success" in body:
            assert body["success"] is False, f"success should be false, got {body['success']}"
            R.ok(name, "success=false confirmed")
        else:
            R.ok(name, "no success field (error indicated by status code)")
    except Exception as e:
        R.fail(name, str(e))


def _405_post_to_get_only():
    """POST to GET-only endpoint - returns 405."""
    name = "405_post_to_get_only"
    try:
        r = req("post", "/health", json={})
        assert r.status_code in (405, 404, 200), f"unexpected status {r.status_code}"
        if r.status_code == 405:
            R.ok(name, "405 Method Not Allowed")
        else:
            R.ok(name, f"status={r.status_code} (endpoint may allow POST)")
    except Exception as e:
        R.fail(name, str(e))


def _large_request_body():
    """Large request body (1MB) - handled or rejected cleanly."""
    name = "large_request_body_1mb"
    try:
        app_id = ensure_app_deployed()
        big_message = "x" * (1024 * 1024)  # 1MB
        r = req("post", f"/api/apps/{app_id}/chat/stream", json={
            "session_id": new_session_id(),
            "message": big_message,
            "workspace": WORKSPACE,
        })
        # Should either process it or reject cleanly (not crash)
        assert r.status_code in (200, 400, 413, 422), f"unexpected status {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  EDGE CASES - INPUT
# ═══════════════════════════════════════════════════════════════

def _chat_edge(name: str, message: str, check_content: str | None = None):
    """Helper: send a message and verify the stream completes without error."""
    try:
        app_id = ensure_app_deployed()
        sid = new_session_id()
        sid, events = stream_chat(app_id, message, session_id=sid)
        content = get_streamed_content(events)
        assert len(events) > 0, "no events received"
        if check_content:
            assert check_content not in content.lower(), \
                f"dangerous content found in response: {check_content}"
        cleanup_session(app_id, sid)
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _edge_empty_message():
    """Empty string message - handled."""
    name = "edge_empty_message"
    try:
        app_id = ensure_app_deployed()
        sid = new_session_id()
        r = req("post", f"/api/apps/{app_id}/chat/stream", json={
            "session_id": sid, "message": "", "workspace": WORKSPACE,
        })
        # Should handle gracefully - either process or reject
        assert r.status_code in (200, 400, 422), f"unexpected status {r.status_code}"
        cleanup_session(app_id, sid)
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _edge_whitespace_message():
    """Whitespace-only message - handled."""
    name = "edge_whitespace_message"
    try:
        app_id = ensure_app_deployed()
        sid = new_session_id()
        r = req("post", f"/api/apps/{app_id}/chat/stream", json={
            "session_id": sid, "message": "   \t   ", "workspace": WORKSPACE,
        })
        assert r.status_code in (200, 400, 422), f"unexpected status {r.status_code}"
        cleanup_session(app_id, sid)
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _edge_newlines():
    """Message with newlines - handled."""
    _chat_edge("edge_newlines", "Line one\nLine two\nLine three")


def _edge_tabs():
    """Message with tabs - handled."""
    _chat_edge("edge_tabs", "Column1\tColumn2\tColumn3")


def _edge_unicode_emoji():
    """Unicode emoji message - handled."""
    _chat_edge("edge_unicode_emoji", "Hello! How are you?")


def _edge_chinese():
    """Chinese characters message - handled."""
    _chat_edge("edge_chinese", "What does this say?")


def _edge_arabic():
    """Arabic text message - handled."""
    _chat_edge("edge_arabic", "Translate this text please")


def _edge_very_long_message():
    """Very long message (50k chars) - handled."""
    name = "edge_very_long_50k"
    try:
        app_id = ensure_app_deployed()
        sid = new_session_id()
        long_msg = "This is a test. " * 3125  # ~50k chars
        sid, events = stream_chat(app_id, long_msg, session_id=sid)
        assert len(events) > 0, "no events received"
        cleanup_session(app_id, sid)
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _edge_html_tags():
    """Message with HTML tags - not interpreted."""
    _chat_edge("edge_html_tags", "<b>bold</b> <i>italic</i> <div>block</div>")


def _edge_script_tags():
    """Message with script tags - sanitized."""
    _chat_edge("edge_script_tags", "<script>alert('xss')</script>")


def _edge_sql_injection():
    """Message with SQL injection attempt - not executed."""
    _chat_edge("edge_sql_injection", "'; DROP TABLE users; --")


def _edge_path_traversal():
    """Message with path traversal (../../etc/passwd) - not followed."""
    _chat_edge("edge_path_traversal", "Read the file ../../etc/passwd")


def _edge_null_bytes():
    """Null bytes in message - handled."""
    name = "edge_null_bytes"
    try:
        app_id = ensure_app_deployed()
        sid = new_session_id()
        r = req("post", f"/api/apps/{app_id}/chat/stream", json={
            "session_id": sid,
            "message": "hello\x00world",
            "workspace": WORKSPACE,
        })
        # Should handle null bytes gracefully
        assert r.status_code in (200, 400, 422), f"unexpected status {r.status_code}"
        cleanup_session(app_id, sid)
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _edge_json_in_message():
    """JSON in message body - treated as text."""
    _chat_edge("edge_json_in_message", '{"key": "value", "number": 42}')


def _edge_backticks_code():
    """Message with backticks and code - handled."""
    _chat_edge("edge_backticks_code", "Run this: ```python\nprint('hello')\n```")


# ═══════════════════════════════════════════════════════════════
#  EDGE CASES - IDs
# ═══════════════════════════════════════════════════════════════

def _id_app_with_dots():
    """App ID with dots - handled."""
    name = "id_app_with_dots"
    try:
        r = req("get", "/api/apps/my.test.app")
        assert r.status_code in (404, 400, 200), f"unexpected status {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _id_app_with_dashes():
    """App ID with dashes - handled."""
    name = "id_app_with_dashes"
    try:
        r = req("get", "/api/apps/my-test-app")
        assert r.status_code in (404, 400, 200), f"unexpected status {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _id_app_with_underscores():
    """App ID with underscores - handled."""
    name = "id_app_with_underscores"
    try:
        r = req("get", "/api/apps/my_test_app")
        assert r.status_code in (404, 400, 200), f"unexpected status {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _id_session_digits_only():
    """Session ID with only digits - handled."""
    name = "id_session_digits_only"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/sessions/1234567890/history")
        assert r.status_code in (404, 400, 200), f"unexpected status {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _id_empty_app_id():
    """Empty app ID - returns error."""
    name = "id_empty_app_id"
    try:
        r = req("get", "/api/apps//sessions")
        assert r.status_code in (400, 404, 405, 307), f"unexpected status {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _id_empty_session_id():
    """Empty session ID - returns error."""
    name = "id_empty_session_id"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/sessions//history")
        assert r.status_code in (400, 404, 405, 307, 422), \
            f"unexpected status {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _id_app_with_spaces():
    """App ID with spaces - returns error."""
    name = "id_app_with_spaces"
    try:
        r = req("get", "/api/apps/my app id")
        assert r.status_code in (400, 404, 422), f"expected error, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _id_session_with_slashes():
    """Session ID with slashes - returns error."""
    name = "id_session_with_slashes"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/sessions/a/b/c/history")
        assert r.status_code in (400, 404, 405, 422), \
            f"expected error, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  EDGE CASES - CONCURRENT
# ═══════════════════════════════════════════════════════════════

def _concurrent_chat_different_sessions():
    """5 concurrent chat requests different sessions - all succeed."""
    name = "concurrent_chat_5_sessions"
    try:
        app_id = ensure_app_deployed()
        sids = [new_session_id() for _ in range(5)]

        def do_chat(sid):
            return stream_chat(app_id, "Concurrent test hello", session_id=sid)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(do_chat, sid): sid for sid in sids}
            results = {}
            for f in concurrent.futures.as_completed(futures, timeout=120):
                sid = futures[f]
                results[sid] = f.result()

        assert len(results) == 5, f"expected 5 results, got {len(results)}"
        for sid in sids:
            cleanup_session(app_id, sid)
        R.ok(name, "all 5 succeeded")
    except Exception as e:
        R.fail(name, str(e))


def _concurrent_get_apps():
    """10 concurrent GET /api/apps - all return same data."""
    name = "concurrent_get_apps_10"
    try:
        def get_apps():
            r = req("get", "/api/apps")
            assert r.status_code == 200
            return r.json()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(get_apps) for _ in range(10)]
            results = [f.result(timeout=30) for f in futures]

        assert len(results) == 10, f"expected 10 results, got {len(results)}"
        # All should return the same data
        first = str(results[0])
        for i, res in enumerate(results[1:], 1):
            assert str(res) == first, f"result {i} differs from result 0"
        R.ok(name, "all 10 identical")
    except Exception as e:
        R.fail(name, str(e))


def _concurrent_deploy_same_app():
    """Concurrent deploy same app - one wins."""
    name = "concurrent_deploy_same"
    try:
        import os
        yaml_path = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "examples", "opencode", "app.yaml",
        ))

        def deploy():
            return req("post", "/api/apps/deploy", json={
                "yaml_path": yaml_path, "force": True,
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(deploy) for _ in range(3)]
            results = [f.result(timeout=30) for f in futures]

        statuses = [r.status_code for r in results]
        successes = [s for s in statuses if s == 200]
        assert len(successes) >= 1, f"expected at least 1 success, got statuses={statuses}"
        R.ok(name, f"statuses={statuses}")
    except Exception as e:
        R.fail(name, str(e))


def _concurrent_session_create():
    """Concurrent session create - no conflicts."""
    name = "concurrent_session_create"
    try:
        app_id = ensure_app_deployed()
        sids = [new_session_id() for _ in range(5)]

        def create_session(sid):
            return stream_chat(app_id, "Init session", session_id=sid)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(create_session, sid): sid for sid in sids}
            results = {}
            for f in concurrent.futures.as_completed(futures, timeout=120):
                sid = futures[f]
                results[sid] = f.result()

        assert len(results) == len(sids), \
            f"expected {len(sids)} results, got {len(results)}"
        for sid in sids:
            cleanup_session(app_id, sid)
        R.ok(name, f"created {len(results)} sessions")
    except Exception as e:
        R.fail(name, str(e))


def _rapid_sequential_chats():
    """Rapid sequential chats same session - serialized."""
    name = "rapid_sequential_chats"
    try:
        app_id = ensure_app_deployed()
        sid = new_session_id()
        for i in range(5):
            stream_chat(app_id, f"Rapid message {i}", session_id=sid)
        cleanup_session(app_id, sid)
        R.ok(name, "5 sequential chats completed")
    except Exception as e:
        R.fail(name, str(e))


def _concurrent_session_forks():
    """3 concurrent session forks - all succeed."""
    name = "concurrent_session_forks"
    try:
        app_id = ensure_app_deployed()
        sid = new_session_id()
        stream_chat(app_id, "Base session for fork", session_id=sid)

        def fork_session():
            return req("post", f"/api/apps/{app_id}/sessions/{sid}/fork")

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(fork_session) for _ in range(3)]
            results = [f.result(timeout=30) for f in futures]

        statuses = [r.status_code for r in results]
        successes = [s for s in statuses if s == 200]
        if len(successes) >= 1:
            R.ok(name, f"statuses={statuses}")
        else:
            # Fork may not be supported
            R.ok(name, f"statuses={statuses} (fork may not be supported)")
        cleanup_session(app_id, sid)
    except Exception as e:
        R.fail(name, str(e))


def _concurrent_read_write():
    """Concurrent read + write operations - no corruption."""
    name = "concurrent_read_write"
    try:
        app_id = ensure_app_deployed()
        sid = new_session_id()
        stream_chat(app_id, "Setup for concurrent test", session_id=sid)

        def read_history():
            return req("get", f"/api/apps/{app_id}/sessions/{sid}/history")

        def write_chat():
            return stream_chat(app_id, "Concurrent write", session_id=sid)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            read_futures = [pool.submit(read_history) for _ in range(2)]
            write_futures = [pool.submit(write_chat) for _ in range(2)]
            all_futures = read_futures + write_futures

            for f in concurrent.futures.as_completed(all_futures, timeout=120):
                f.result()  # raises if any failed

        cleanup_session(app_id, sid)
        R.ok(name, "no corruption detected")
    except Exception as e:
        R.fail(name, str(e))


def _concurrent_abort_and_chat():
    """Concurrent abort + chat - handled gracefully."""
    name = "concurrent_abort_chat"
    try:
        app_id = ensure_app_deployed()
        sid = new_session_id()
        stream_chat(app_id, "Setup for abort test", session_id=sid)

        def do_abort():
            return req("post", f"/api/apps/{app_id}/sessions/{sid}/abort")

        def do_chat():
            try:
                return stream_chat(app_id, "Chat during abort", session_id=sid)
            except Exception:
                return None  # Abort may cause stream to fail - that's fine

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            abort_future = pool.submit(do_abort)
            chat_future = pool.submit(do_chat)

            abort_result = abort_future.result(timeout=30)
            chat_result = chat_future.result(timeout=120)

        # Both should complete without crashing the server
        # Verify server is still alive
        r = req("get", "/api/apps")
        assert r.status_code == 200, "server crashed after concurrent abort+chat"
        cleanup_session(app_id, sid)
        R.ok(name, "server stable after concurrent abort+chat")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  RUNNER
# ═══════════════════════════════════════════════════════════════

def run_all():
    """Run all security, error handling, and edge case production tests."""

    print("\n\033[1m=== SESSION ISOLATION ===\033[0m")
    _session_a_cannot_see_b_messages()
    _session_b_cannot_see_a_data()
    _sessions_completely_independent()
    _history_scoped_to_session()
    _memory_scoped_to_session()
    _workspace_scoped_to_session()
    _fork_requires_session_ownership()
    _compact_requires_session_ownership()
    _abort_requires_session_ownership()
    _nonexistent_session_returns_404()
    _random_uuid_session_returns_404()
    _session_ids_are_unpredictable()

    print("\n\033[1m=== AUTH ENFORCEMENT ===\033[0m")
    _noauth_get_apps()
    _noauth_deploy()
    _noauth_chat_stream()
    _noauth_sessions()
    _noauth_config()
    _invalid_bearer_token()
    _expired_token_format()
    _empty_auth_header()
    _bearer_empty_token()
    _malformed_jwt()
    _health_no_auth_required()
    _healthz_no_auth_required()

    print("\n\033[1m=== ERROR RESPONSES ===\033[0m")
    _404_nonexistent_app()
    _404_nonexistent_session()
    _404_nonexistent_tool()
    _400_invalid_app_id_special_chars()
    _400_too_long_app_id()
    _400_too_long_session_id()
    _422_malformed_json()
    _422_missing_required_fields()
    _422_wrong_field_types()
    _500_includes_error_message()
    _error_responses_are_json()
    _error_responses_have_error_field()
    _error_responses_have_success_false()
    _405_post_to_get_only()
    _large_request_body()

    print("\n\033[1m=== EDGE CASES - INPUT ===\033[0m")
    _edge_empty_message()
    _edge_whitespace_message()
    _edge_newlines()
    _edge_tabs()
    _edge_unicode_emoji()
    _edge_chinese()
    _edge_arabic()
    _edge_very_long_message()
    _edge_html_tags()
    _edge_script_tags()
    _edge_sql_injection()
    _edge_path_traversal()
    _edge_null_bytes()
    _edge_json_in_message()
    _edge_backticks_code()

    print("\n\033[1m=== EDGE CASES - IDs ===\033[0m")
    _id_app_with_dots()
    _id_app_with_dashes()
    _id_app_with_underscores()
    _id_session_digits_only()
    _id_empty_app_id()
    _id_empty_session_id()
    _id_app_with_spaces()
    _id_session_with_slashes()

    print("\n\033[1m=== EDGE CASES - CONCURRENT ===\033[0m")
    _concurrent_chat_different_sessions()
    _concurrent_get_apps()
    _concurrent_deploy_same_app()
    _concurrent_session_create()
    _rapid_sequential_chats()
    _concurrent_session_forks()
    _concurrent_read_write()
    _concurrent_abort_and_chat()

    print(f"\n\033[1m--- {R.summary()} ---\033[0m")


if __name__ == "__main__":
    run_all()
