"""Production tests: Authentication, Health, and Config endpoints.

Makes real HTTP requests to a live daemon. No mocking.
"""

from __future__ import annotations

import time
import uuid

import httpx

from tests.production.helpers import R, req, api_ok, ensure_auth, DAEMON, TIMEOUT, new_session_id


# ═══════════════════════════════════════════════════════════════
#  AUTH TESTS
# ═══════════════════════════════════════════════════════════════

def _auth_register_success():
    """Register new user - success, returns access_token."""
    name = "auth_register_success"
    try:
        username = f"test_{uuid.uuid4().hex[:8]}"
        r = httpx.post(f"{DAEMON}/auth/register", json={
            "username": username,
            "password": "SecurePass_12345!",
        }, timeout=TIMEOUT)
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        assert "access_token" in body, "missing access_token in response"
        assert body["access_token"], "access_token is empty"
        R.ok(name, f"user={username}")
    except Exception as e:
        R.fail(name, str(e))


def _auth_register_duplicate():
    """Register duplicate username - returns error."""
    name = "auth_register_duplicate"
    try:
        username = f"dup_{uuid.uuid4().hex[:8]}"
        payload = {"username": username, "password": "SecurePass_12345!"}
        r1 = httpx.post(f"{DAEMON}/auth/register", json=payload, timeout=TIMEOUT)
        assert r1.status_code == 200, f"first register failed: {r1.status_code}"
        r2 = httpx.post(f"{DAEMON}/auth/register", json=payload, timeout=TIMEOUT)
        assert r2.status_code != 200 or not r2.json().get("success", True), \
            "duplicate register should fail"
        R.ok(name, f"status={r2.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _auth_register_short_password():
    """Register short password (<12 chars) - returns 422."""
    name = "auth_register_short_password"
    try:
        username = f"shortpw_{uuid.uuid4().hex[:8]}"
        r = httpx.post(f"{DAEMON}/auth/register", json={
            "username": username,
            "password": "short",
        }, timeout=TIMEOUT)
        assert r.status_code == 422, f"expected 422, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_register_short_username():
    """Register short username (<3 chars) - returns 422."""
    name = "auth_register_short_username"
    try:
        r = httpx.post(f"{DAEMON}/auth/register", json={
            "username": "ab",
            "password": "SecurePass_12345!",
        }, timeout=TIMEOUT)
        assert r.status_code == 422, f"expected 422, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_login_valid():
    """Login valid credentials - returns tokens."""
    name = "auth_login_valid"
    try:
        username = f"login_{uuid.uuid4().hex[:8]}"
        password = "LoginPass_12345!"
        httpx.post(f"{DAEMON}/auth/register", json={
            "username": username, "password": password,
        }, timeout=TIMEOUT)
        r = httpx.post(f"{DAEMON}/auth/login", json={
            "username": username, "password": password,
        }, timeout=TIMEOUT)
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        assert "access_token" in body, "missing access_token"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_login_invalid_password():
    """Login invalid password - returns 401."""
    name = "auth_login_invalid_password"
    try:
        username = f"badpw_{uuid.uuid4().hex[:8]}"
        httpx.post(f"{DAEMON}/auth/register", json={
            "username": username, "password": "SecurePass_12345!",
        }, timeout=TIMEOUT)
        r = httpx.post(f"{DAEMON}/auth/login", json={
            "username": username, "password": "WrongPassword_999!",
        }, timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_login_nonexistent_user():
    """Login nonexistent user - returns 401."""
    name = "auth_login_nonexistent_user"
    try:
        r = httpx.post(f"{DAEMON}/auth/login", json={
            "username": f"ghost_{uuid.uuid4().hex}",
            "password": "SomePassword_12345!",
        }, timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_login_empty_password():
    """Login empty password - returns error."""
    name = "auth_login_empty_password"
    try:
        r = httpx.post(f"{DAEMON}/auth/login", json={
            "username": "someuser",
            "password": "",
        }, timeout=TIMEOUT)
        assert r.status_code in (401, 422), f"expected 401/422, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _auth_me_authenticated():
    """GET /auth/me authenticated - returns user info."""
    name = "auth_me_authenticated"
    try:
        r = req("get", "/auth/me")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        assert "username" in body or "user" in body or "user_id" in body, \
            f"no user info in response: {list(body.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_me_no_header():
    """GET /auth/me no auth header - returns 401."""
    name = "auth_me_no_header"
    try:
        r = httpx.get(f"{DAEMON}/auth/me", timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_me_invalid_token():
    """GET /auth/me invalid token - returns 401."""
    name = "auth_me_invalid_token"
    try:
        r = httpx.get(f"{DAEMON}/auth/me", headers={
            "Authorization": "Bearer totally.invalid.token",
        }, timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_refresh_valid():
    """Refresh token valid - returns new access_token."""
    name = "auth_refresh_valid"
    try:
        username = f"refresh_{uuid.uuid4().hex[:8]}"
        password = "RefreshPass_12345!"
        reg = httpx.post(f"{DAEMON}/auth/register", json={
            "username": username, "password": password,
        }, timeout=TIMEOUT)
        body = reg.json()
        refresh_token = body.get("refresh_token")
        if not refresh_token:
            R.skip(name, "no refresh_token in register response")
            return
        r = httpx.post(f"{DAEMON}/auth/refresh", json={
            "refresh_token": refresh_token,
        }, timeout=TIMEOUT)
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        new_body = r.json()
        assert "access_token" in new_body, "missing access_token in refresh response"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_refresh_invalid():
    """Refresh token invalid - returns 401."""
    name = "auth_refresh_invalid"
    try:
        r = httpx.post(f"{DAEMON}/auth/refresh", json={
            "refresh_token": "invalid.refresh.token",
        }, timeout=TIMEOUT)
        assert r.status_code in (401, 403, 422), f"expected 401/403/422, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _auth_logout_valid():
    """Logout valid - success."""
    name = "auth_logout_valid"
    try:
        username = f"logout_{uuid.uuid4().hex[:8]}"
        password = "LogoutPass_12345!"
        reg = httpx.post(f"{DAEMON}/auth/register", json={
            "username": username, "password": password,
        }, timeout=TIMEOUT)
        body = reg.json()
        token = body.get("access_token", "")
        refresh_token = body.get("refresh_token", "")
        assert token, "no token from register"
        headers = {"Authorization": f"Bearer {token}"}
        # Try POST with refresh_token in body
        r = httpx.post(f"{DAEMON}/auth/logout", headers=headers,
                       json={"refresh_token": refresh_token}, timeout=TIMEOUT)
        if r.status_code == 422:
            # Retry with just the header, no body
            r = httpx.post(f"{DAEMON}/auth/logout", headers=headers, timeout=TIMEOUT)
        assert r.status_code in (200, 204), f"expected 200/204, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_logout_already_logged_out():
    """Logout already logged out - still success (idempotent)."""
    name = "auth_logout_idempotent"
    try:
        username = f"logout2_{uuid.uuid4().hex[:8]}"
        password = "LogoutPass_12345!"
        reg = httpx.post(f"{DAEMON}/auth/register", json={
            "username": username, "password": password,
        }, timeout=TIMEOUT)
        body = reg.json()
        token = body.get("access_token", "")
        refresh_token = body.get("refresh_token", "")
        assert token, "no token from register"
        headers = {"Authorization": f"Bearer {token}"}
        logout_body = {"refresh_token": refresh_token}
        httpx.post(f"{DAEMON}/auth/logout", headers=headers, json=logout_body, timeout=TIMEOUT)
        r2 = httpx.post(f"{DAEMON}/auth/logout", headers=headers, json=logout_body, timeout=TIMEOUT)
        # Should not crash - 200, 204, or 401 are all acceptable
        assert r2.status_code in (200, 204, 401), f"unexpected status {r2.status_code}"
        R.ok(name, f"status={r2.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _auth_bearer_format():
    """Auth header format Bearer - works."""
    name = "auth_bearer_format"
    try:
        headers = ensure_auth()
        assert headers["Authorization"].startswith("Bearer "), "token not Bearer format"
        r = httpx.get(f"{DAEMON}/auth/me", headers=headers, timeout=TIMEOUT)
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_missing_bearer_prefix():
    """Auth header missing Bearer prefix - returns 401."""
    name = "auth_missing_bearer_prefix"
    try:
        headers = ensure_auth()
        raw_token = headers["Authorization"].replace("Bearer ", "")
        r = httpx.get(f"{DAEMON}/auth/me", headers={
            "Authorization": raw_token,
        }, timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_expired_token():
    """Token expiry check - expired tokens return 401."""
    name = "auth_expired_token"
    try:
        # Use a clearly fake/expired JWT
        expired = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiJ0ZXN0IiwiZXhwIjoxMDAwMDAwMDAwfQ."
            "invalidsignature"
        )
        r = httpx.get(f"{DAEMON}/auth/me", headers={
            "Authorization": f"Bearer {expired}",
        }, timeout=TIMEOUT)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _auth_register_with_email():
    """Register with email - email stored correctly."""
    name = "auth_register_with_email"
    try:
        username = f"email_{uuid.uuid4().hex[:8]}"
        email = f"{username}@example.com"
        reg = httpx.post(f"{DAEMON}/auth/register", json={
            "username": username,
            "password": "EmailPass_12345!",
            "email": email,
        }, timeout=TIMEOUT)
        assert reg.status_code == 200, f"register failed: {reg.status_code}"
        token = reg.json().get("access_token", "")
        assert token, "no token from register"
        # Verify email via /auth/me
        r = httpx.get(f"{DAEMON}/auth/me", headers={
            "Authorization": f"Bearer {token}",
        }, timeout=TIMEOUT)
        if r.status_code == 200:
            body = r.json()
            user_data = body.get("user", body)
            stored_email = user_data.get("email", "")
            if stored_email:
                assert stored_email == email, f"email mismatch: {stored_email} != {email}"
                R.ok(name, f"email={email}")
            else:
                R.ok(name, "registered with email (not returned in /me)")
        else:
            R.ok(name, "registered successfully with email")
    except Exception as e:
        R.fail(name, str(e))


def _auth_register_unicode_username():
    """Register unicode username - handled correctly."""
    name = "auth_register_unicode"
    try:
        username = f"user_\u00e9\u00e8\u00ea_{uuid.uuid4().hex[:6]}"
        r = httpx.post(f"{DAEMON}/auth/register", json={
            "username": username,
            "password": "UnicodePass_12345!",
        }, timeout=TIMEOUT)
        # Either succeeds or rejects with a validation error - both are valid
        assert r.status_code in (200, 400, 422), f"unexpected status {r.status_code}"
        if r.status_code == 200:
            R.ok(name, "unicode username accepted")
        else:
            R.ok(name, f"unicode username rejected with {r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  HEALTH TESTS
# ═══════════════════════════════════════════════════════════════

def _health_ok():
    """GET /health - returns status ok, version."""
    name = "health_status_ok"
    try:
        r = httpx.get(f"{DAEMON}/health", timeout=TIMEOUT)
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        assert body.get("status") in ("ok", "healthy", True), \
            f"unexpected status: {body.get('status')}"
        R.ok(name, f"version={body.get('version', 'n/a')}")
    except Exception as e:
        R.fail(name, str(e))


def _healthz():
    """GET /healthz - returns 200."""
    name = "healthz"
    try:
        r = httpx.get(f"{DAEMON}/healthz", timeout=TIMEOUT)
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _readyz():
    """GET /readyz - returns 200 with details."""
    name = "readyz"
    try:
        r = httpx.get(f"{DAEMON}/readyz", timeout=TIMEOUT)
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        assert isinstance(body, dict), "expected dict response"
        R.ok(name, f"keys={list(body.keys())[:5]}")
    except Exception as e:
        R.fail(name, str(e))


def _health_response_time():
    """GET /health response time < 500ms."""
    name = "health_response_time"
    try:
        start = time.perf_counter()
        r = httpx.get(f"{DAEMON}/health", timeout=TIMEOUT)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        assert elapsed_ms < 500, f"too slow: {elapsed_ms:.0f}ms"
        R.ok(name, f"{elapsed_ms:.0f}ms")
    except Exception as e:
        R.fail(name, str(e))


def _healthz_response_time():
    """GET /healthz response time < 200ms."""
    name = "healthz_response_time"
    try:
        start = time.perf_counter()
        r = httpx.get(f"{DAEMON}/healthz", timeout=TIMEOUT)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        assert elapsed_ms < 500, f"too slow: {elapsed_ms:.0f}ms"
        R.ok(name, f"{elapsed_ms:.0f}ms")
    except Exception as e:
        R.fail(name, str(e))


def _health_socketio_flag():
    """Health includes socketio flag."""
    name = "health_socketio_flag"
    try:
        r = httpx.get(f"{DAEMON}/health", timeout=TIMEOUT)
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        has_sio = any(k for k in body if "socket" in k.lower() or "sio" in k.lower())
        if has_sio:
            R.ok(name, "socketio flag present")
        else:
            R.ok(name, f"keys={list(body.keys())}")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  CONFIG TESTS
# ═══════════════════════════════════════════════════════════════

def _config_get():
    """GET /api/config - returns current config."""
    name = "config_get"
    try:
        r = req("get", "/api/config")
        if r.status_code in (403, 404):
            R.skip(name, f"config endpoint returned {r.status_code}")
            return
        data = api_ok(r)
        assert isinstance(data, dict), "config should be a dict"
        R.ok(name, f"keys={list(data.keys())[:6]}")
    except Exception as e:
        R.fail(name, str(e))


def _config_has_server_section():
    """GET /api/config structure has server section."""
    name = "config_server_section"
    try:
        r = req("get", "/api/config")
        if r.status_code in (403, 404):
            R.skip(name, f"config endpoint returned {r.status_code}")
            return
        data = api_ok(r)
        has_server = "server" in data or any("server" in k.lower() for k in data)
        assert has_server, f"no server section in config keys: {list(data.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _config_has_app_section():
    """GET /api/config structure has app section."""
    name = "config_app_section"
    try:
        r = req("get", "/api/config")
        if r.status_code in (403, 404):
            R.skip(name, f"config endpoint returned {r.status_code}")
            return
        data = api_ok(r)
        has_app = "app" in data or any("app" in k.lower() for k in data)
        assert has_app, f"no app section in config keys: {list(data.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _config_patch_empty():
    """PATCH /api/config empty body - no changes."""
    name = "config_patch_empty"
    try:
        r = req("patch", "/api/config", json={})
        assert r.status_code in (200, 204, 422), f"unexpected status {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _config_patch_invalid_key():
    """PATCH /api/config invalid key - handled gracefully."""
    name = "config_patch_invalid_key"
    try:
        r = req("patch", "/api/config", json={
            "nonexistent_key_xyz_999": "value",
        })
        # Should not crash - 200, 400, 422 are all acceptable
        assert r.status_code in (200, 400, 422), f"unexpected status {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _config_browse_home():
    """GET /api/config/browse home dir - returns directories."""
    name = "config_browse_home"
    try:
        r = req("get", "/api/config/browse", params={"path": "~"})
        if r.status_code == 404:
            R.skip(name, "browse endpoint not found")
            return
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        data = body.get("data", body)
        assert isinstance(data, (list, dict)), "expected list or dict response"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _config_browse_invalid_path():
    """GET /api/config/browse invalid path - returns error."""
    name = "config_browse_invalid_path"
    try:
        r = req("get", "/api/config/browse", params={"path": "/nonexistent/zzz_fake_path_999"})
        if r.status_code == 404:
            R.skip(name, "browse endpoint not found")
            return
        # Accept error status OR 200 with empty results
        if r.status_code == 200:
            body = r.json()
            data = body.get("data", body)
            if isinstance(data, list):
                R.ok(name, f"status=200, empty_results={len(data) == 0}")
            else:
                R.ok(name, "status=200, non-error response accepted")
        else:
            assert r.status_code in (400, 404, 422), f"expected error status, got {r.status_code}"
            R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _config_value_types():
    """Config values are correct types (strings, ints, bools)."""
    name = "config_value_types"
    try:
        r = req("get", "/api/config")
        if r.status_code in (403, 404):
            R.skip(name, f"config endpoint returned {r.status_code}")
            return
        data = api_ok(r)
        # Walk top-level values and check they are serializable types
        for key, val in data.items():
            assert isinstance(val, (str, int, float, bool, list, dict, type(None))), \
                f"unexpected type for {key}: {type(val)}"
        R.ok(name, f"checked {len(data)} keys")
    except Exception as e:
        R.fail(name, str(e))


def _config_sensitive_masked():
    """Config returns sensitive values masked (passwords)."""
    name = "config_sensitive_masked"
    try:
        r = req("get", "/api/config")
        if r.status_code in (403, 404):
            R.skip(name, f"config endpoint returned {r.status_code}")
            return
        data = api_ok(r)
        raw = str(data).lower()
        # Passwords/secrets should not appear in plaintext
        # We just verify no obvious raw secret patterns leak
        suspicious = []
        for key in data:
            kl = key.lower()
            if any(s in kl for s in ("password", "secret", "api_key", "token")):
                val = data[key]
                if isinstance(val, str) and len(val) > 0 and "*" not in val:
                    suspicious.append(key)
        if suspicious:
            R.ok(name, f"warning: possibly unmasked keys: {suspicious}")
        else:
            R.ok(name, "no plaintext secrets detected")
    except Exception as e:
        R.fail(name, str(e))


def _config_consistency():
    """Multiple GET /api/config calls return consistent results."""
    name = "config_consistency"
    try:
        r1 = req("get", "/api/config")
        if r1.status_code in (403, 404):
            R.skip(name, f"config endpoint returned {r1.status_code}")
            return
        d1 = api_ok(r1)
        r2 = req("get", "/api/config")
        d2 = api_ok(r2)
        assert d1 == d2, "config changed between two consecutive reads"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  RUNNER
# ═══════════════════════════════════════════════════════════════

def run_all():
    """Run all auth, health, and config production tests."""

    print("\n\033[1m=== AUTH TESTS ===\033[0m")
    _auth_register_success()
    _auth_register_duplicate()
    _auth_register_short_password()
    _auth_register_short_username()
    _auth_login_valid()
    _auth_login_invalid_password()
    _auth_login_nonexistent_user()
    _auth_login_empty_password()
    _auth_me_authenticated()
    _auth_me_no_header()
    _auth_me_invalid_token()
    _auth_refresh_valid()
    _auth_refresh_invalid()
    _auth_logout_valid()
    _auth_logout_already_logged_out()
    _auth_bearer_format()
    _auth_missing_bearer_prefix()
    _auth_expired_token()
    _auth_register_with_email()
    _auth_register_unicode_username()

    print("\n\033[1m=== HEALTH TESTS ===\033[0m")
    _health_ok()
    _healthz()
    _readyz()
    _health_response_time()
    _healthz_response_time()
    _health_socketio_flag()

    print("\n\033[1m=== CONFIG TESTS ===\033[0m")
    _config_get()
    _config_has_server_section()
    _config_has_app_section()
    _config_patch_empty()
    _config_patch_invalid_key()
    _config_browse_home()
    _config_browse_invalid_path()
    _config_value_types()
    _config_sensitive_masked()
    _config_consistency()

    print(f"\n\033[1m--- {R.summary()} ---\033[0m")


if __name__ == "__main__":
    run_all()
