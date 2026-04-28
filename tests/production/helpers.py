"""Shared test infrastructure for production tests.

Provides authenticated HTTP client, SSE parsing, assertions,
and test app deployment/cleanup.
"""

from __future__ import annotations

import json
import os
import sys

# Fix Windows console encoding for emoji/unicode in test output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

# ── Config ────────────────────────────────────────────────────

DAEMON = os.environ.get("DIGITORN_TEST_DAEMON", "http://127.0.0.1:8000")
TEST_APP_YAML = os.environ.get(
    "DIGITORN_TEST_APP",
    os.path.join(os.path.dirname(__file__), "..", "..", "examples", "opencode", "app.yaml"),
)
WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TIMEOUT = 30.0
STREAM_TIMEOUT = 120.0
LLM_TIMEOUT = 90.0


# ── Results tracker ───────────────────────────────────────────

@dataclass
class TestResults:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failed) + len(self.skipped)

    def ok(self, name: str, detail: str = ""):
        self.passed.append(name)
        print(f"  \033[32mOK\033[0m   {name}" + (f"  ({detail})" if detail else ""))

    def fail(self, name: str, detail: str = ""):
        self.failed.append(name)
        self.errors.append(f"{name}: {detail}")
        print(f"  \033[31mFAIL\033[0m {name}  -- {detail}")

    def skip(self, name: str, detail: str = ""):
        self.skipped.append(name)
        print(f"  \033[33mSKIP\033[0m {name}  -- {detail}")

    def summary(self) -> str:
        p, f, s = len(self.passed), len(self.failed), len(self.skipped)
        return f"{p} passed, {f} failed, {s} skipped (total {self.total})"


# Global results - each test file adds to this
R = TestResults()


# ── Authenticated HTTP client ─────────────────────────────────

_auth_headers: dict[str, str] = {}
_current_user: dict[str, str] = {}
_token_created_at: float = 0.0
_TOKEN_MAX_AGE = 600.0  # Refresh token every 10 min (JWT expires at 15)


def ensure_auth() -> dict[str, str]:
    """Register/login and return auth headers. Auto-refreshes expired tokens."""
    global _auth_headers, _current_user, _token_created_at

    # Check if token needs refresh
    if _auth_headers and (time.time() - _token_created_at) < _TOKEN_MAX_AGE:
        return _auth_headers

    # Force re-auth if token is stale
    if _auth_headers and _current_user.get("refresh_token"):
        try:
            r = httpx.post(f"{DAEMON}/auth/refresh", json={
                "refresh_token": _current_user["refresh_token"],
            }, timeout=TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                _auth_headers["Authorization"] = f"Bearer {data['access_token']}"
                _current_user["token"] = data["access_token"]
                _token_created_at = time.time()
                return _auth_headers
        except Exception:
            pass  # Fall through to full re-auth

    password = "ProdTest_Secure_12345!"
    existing_username = _current_user.get("username")

    # Strategy 1: Login with known credentials
    if existing_username:
        r = httpx.post(f"{DAEMON}/auth/login", json={
            "username": existing_username,
            "password": password,
        }, timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            token = data["access_token"]
            username = existing_username
            _auth_headers = {"Authorization": f"Bearer {token}"}
            _current_user["token"] = token
            _current_user["refresh_token"] = data.get("refresh_token", "")
            _token_created_at = time.time()
            return _auth_headers

    # Strategy 2: Register new user
    username = f"prodtest_{uuid.uuid4().hex[:8]}"
    r = httpx.post(f"{DAEMON}/auth/register", json={
        "username": username,
        "password": password,
        "email": f"{username}@test.local",
    }, timeout=TIMEOUT)

    if r.status_code == 200:
        data = r.json()
        token = data["access_token"]
    else:
        # Strategy 3: Fallback to testuser
        r = httpx.post(f"{DAEMON}/auth/login", json={
            "username": "testuser",
            "password": "testpass12345!",
        }, timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"Cannot authenticate: {r.status_code} {r.text[:200]}")
        data = r.json()
        token = data["access_token"]
        username = "testuser"

    _auth_headers = {"Authorization": f"Bearer {token}"}
    _current_user = {
        "username": username,
        "user_id": data.get("user_id", ""),
        "token": token,
        "refresh_token": data.get("refresh_token", ""),
    }
    _token_created_at = time.time()
    return _auth_headers


def req(method: str, path: str, **kwargs) -> httpx.Response:
    """Make an authenticated request."""
    kwargs.setdefault("timeout", TIMEOUT)
    headers = kwargs.pop("headers", {})
    headers.update(ensure_auth())
    kwargs["headers"] = headers
    return getattr(httpx, method.lower())(f"{DAEMON}{path}", **kwargs)


def api_ok(resp: httpx.Response) -> dict:
    """Assert 200 + success=true, return data.

    Handles both {success:true, data:{...}} and direct config dict responses.
    """
    if resp.status_code != 200:
        raise AssertionError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    # Standard envelope: {success: true, data: {...}}
    if isinstance(body, dict) and "success" in body:
        if not body.get("success"):
            raise AssertionError(f"API error: {body.get('error', 'unknown')[:300]}")
        return body.get("data", body)
    # Direct dict/list response (no envelope)
    if isinstance(body, (dict, list)):
        return body
    raise AssertionError(f"Unexpected response type: {type(body)}")


def new_session_id() -> str:
    return str(uuid.uuid4())


# ── SSE helpers ───────────────────────────────────────────────

_last_llm_call: float = 0.0
_LLM_MIN_INTERVAL = 3.0  # Minimum seconds between LLM calls to avoid rate limiting


def stream_chat(app_id: str, message: str, session_id: str | None = None,
                timeout: float = STREAM_TIMEOUT) -> tuple[str, list[dict]]:
    """Send a chat message via SSE stream. Returns (session_id, events).

    Auto-throttles to avoid rate limiting (429).
    """
    global _last_llm_call
    # Throttle LLM calls
    elapsed = time.time() - _last_llm_call
    if elapsed < _LLM_MIN_INTERVAL:
        time.sleep(_LLM_MIN_INTERVAL - elapsed)
    _last_llm_call = time.time()

    sid = session_id or new_session_id()
    headers = dict(ensure_auth())

    with httpx.stream(
        "POST", f"{DAEMON}/api/apps/{app_id}/chat/stream",
        headers=headers,
        json={"session_id": sid, "message": message, "workspace": WORKSPACE},
        timeout=timeout,
    ) as resp:
        if resp.status_code == 429:
            # Rate limited - wait and retry once
            time.sleep(20)
            _last_llm_call = time.time()
            with httpx.stream(
                "POST", f"{DAEMON}/api/apps/{app_id}/chat/stream",
                headers=headers,
                json={"session_id": sid, "message": message, "workspace": WORKSPACE},
                timeout=timeout,
            ) as resp2:
                if resp2.status_code != 200:
                    raise AssertionError(f"Stream HTTP {resp2.status_code} (after rate limit retry)")
                events = _collect_sse(resp2)
                return sid, events
        if resp.status_code != 200:
            raise AssertionError(f"Stream HTTP {resp.status_code}")
        events = _collect_sse(resp)

    return sid, events


def _collect_sse(resp: httpx.Response) -> list[dict]:
    """Parse SSE events from streaming response.

    Handles multi-line data fields and large JSON payloads that may
    arrive across multiple chunks.
    """
    events = []
    event_type = ""
    data_lines: list[str] = []
    line_buf = ""

    for chunk in resp.iter_bytes():
        line_buf += chunk.decode("utf-8", errors="replace")

        while "\n" in line_buf:
            line, line_buf = line_buf.split("\n", 1)
            line = line.rstrip("\r")

            if line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                data_lines.append(line[6:])
            elif line == "" and event_type:
                # Empty line = end of event
                data_buf = "\n".join(data_lines)
                if data_buf:
                    try:
                        data = json.loads(data_buf) if data_buf != "[DONE]" else {}
                    except json.JSONDecodeError:
                        data = {"raw": data_buf}
                    events.append({"event": event_type, "data": data})
                event_type = ""
                data_lines = []
    return events


def get_streamed_content(events: list[dict]) -> str:
    """Extract full content from SSE events (tokens + result)."""
    # First try result.content
    result = next((e for e in events if e["event"] == "result"), None)
    if result and result["data"].get("content"):
        return result["data"]["content"]
    # Fallback to accumulated tokens
    tokens = [e for e in events if e["event"] == "token"]
    return "".join(e["data"].get("delta", "") for e in tokens)


def get_result(events: list[dict]) -> dict | None:
    """Get result event data."""
    r = next((e for e in events if e["event"] == "result"), None)
    return r["data"] if r else None


def get_error(events: list[dict]) -> str | None:
    """Get error from events."""
    err = next((e for e in events if e["event"] == "error"), None)
    if err:
        return err["data"].get("error", "unknown")
    result = get_result(events)
    if result and result.get("error"):
        return result["error"]
    return None


def get_events_by_type(events: list[dict], event_type: str) -> list[dict]:
    """Filter events by type."""
    return [e for e in events if e["event"] == event_type]


# ── App deployment helpers ────────────────────────────────────

_deployed_app_id: str = ""


def ensure_app_deployed() -> str:
    """Deploy the test app if not already deployed. Returns app_id.

    Checks via GET first to avoid burning the deploy rate limit (30 req/60s).
    Retries on 429 with the server-provided retry_after delay.
    """
    global _deployed_app_id
    if _deployed_app_id:
        # Check if still deployed (GET is not rate-limited on deploy quota)
        r = req("get", f"/api/apps/{_deployed_app_id}")
        if r.status_code == 200:
            return _deployed_app_id

    # Try to discover app_id from YAML before deploying
    yaml_path = os.path.abspath(TEST_APP_YAML)
    try:
        import yaml as _yaml
        with open(yaml_path) as _f:
            _cfg = _yaml.safe_load(_f)
        _peek_id = (_cfg or {}).get("app", {}).get("app_id", "")
    except Exception:
        _peek_id = ""

    if _peek_id:
        r = req("get", f"/api/apps/{_peek_id}")
        if r.status_code == 200:
            _deployed_app_id = _peek_id
            return _deployed_app_id

    # Actually need to deploy - retry on 429
    for _attempt in range(3):
        r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
        if r.status_code == 429:
            retry_after = r.json().get("retry_after", 30)
            time.sleep(min(retry_after + 1, 60))
            continue
        data = api_ok(r)
        _deployed_app_id = data.get("app_id", "")
        assert _deployed_app_id, "No app_id in deploy response"
        return _deployed_app_id

    # All retries exhausted
    raise AssertionError(f"Deploy failed after 3 retries (429): {r.text[:300]}")


def cleanup_app():
    """Undeploy the test app."""
    global _deployed_app_id
    if _deployed_app_id:
        try:
            req("delete", f"/api/apps/{_deployed_app_id}")
        except Exception:
            pass
        _deployed_app_id = ""


def cleanup_session(app_id: str, session_id: str):
    """Delete a test session."""
    try:
        req("delete", f"/api/apps/{app_id}/sessions/{session_id}")
    except Exception:
        pass
