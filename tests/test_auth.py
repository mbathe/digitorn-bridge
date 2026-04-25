"""Comprehensive auth system tests.

Covers JWT, local provider, middleware, RBAC roles,
session persistence, and API key generation.
"""

from __future__ import annotations

import hashlib
import time

import pytest


# ── JWT Tests ─────────────────────────────────────────────────────


class TestJWT:
    """JWT token generation and verification."""

    @pytest.fixture
    def jwt_service(self):
        from digitorn.core.auth.jwt import JWTService
        return JWTService(secret_key="test_secret_key_at_least_32_chars_long")

    def test_generate_access_token(self, jwt_service):
        token = jwt_service.generate_access_token(user_id="u1")
        assert isinstance(token, str)
        assert len(token) > 50

    def test_verify_access_token(self, jwt_service):
        token = jwt_service.generate_access_token(
            user_id="u1",
            email="test@test.com",
            roles=["developer"],
            permissions=["apps:read"],
        )
        payload = jwt_service.verify(token)
        assert payload.user_id == "u1"
        assert payload.email == "test@test.com"
        assert payload.roles == ["developer"]
        assert payload.permissions == ["apps:read"]
        assert payload.token_type == "access"

    def test_access_token_has_expiry(self, jwt_service):
        token = jwt_service.generate_access_token(user_id="u1")
        payload = jwt_service.verify(token)
        assert payload.expires_at > time.time()
        assert payload.expires_at < time.time() + 901  # 15 min default

    def test_expired_token_rejected(self):
        from digitorn.core.auth.jwt import JWTService
        jwt = JWTService(secret_key="test_secret_key_at_least_32_chars_long", access_ttl=0)
        token = jwt.generate_access_token(user_id="u1")
        import jwt as pyjwt
        with pytest.raises(pyjwt.ExpiredSignatureError):
            jwt.verify(token)

    def test_wrong_secret_rejected(self, jwt_service):
        from digitorn.core.auth.jwt import JWTService
        other = JWTService(secret_key="different_secret_key_at_least_32_chars")
        token = jwt_service.generate_access_token(user_id="u1")
        import jwt as pyjwt
        with pytest.raises(pyjwt.InvalidSignatureError):
            other.verify(token)

    def test_generate_refresh_token(self, jwt_service):
        token, token_hash = jwt_service.generate_refresh_token("u1")
        assert isinstance(token, str)
        assert isinstance(token_hash, str)
        assert len(token_hash) == 64  # SHA-256 hex

    def test_refresh_token_has_correct_type(self, jwt_service):
        token, _ = jwt_service.generate_refresh_token("u1")
        payload = jwt_service.verify(token)
        assert payload.token_type == "refresh"
        assert payload.user_id == "u1"

    def test_refresh_token_hash_is_deterministic(self, jwt_service):
        token, hash1 = jwt_service.generate_refresh_token("u1")
        hash2 = jwt_service.hash_token(token)
        assert hash1 == hash2

    def test_access_token_with_app_scope(self, jwt_service):
        token = jwt_service.generate_access_token(user_id="u1", app_id="my-app")
        payload = jwt_service.verify(token)
        assert payload.app_id == "my-app"

    def test_access_token_with_display_name(self, jwt_service):
        token = jwt_service.generate_access_token(
            user_id="u1", display_name="John Doe",
        )
        payload = jwt_service.verify(token)
        assert payload.display_name == "John Doe"

    def test_access_token_with_extra_claims(self, jwt_service):
        token = jwt_service.generate_access_token(
            user_id="u1", extra={"department": "engineering"},
        )
        # Extra claims are in the raw JWT but not in TokenPayload
        import jwt as pyjwt
        decoded = pyjwt.decode(token, options={"verify_signature": False})
        assert decoded["department"] == "engineering"

    def test_multiple_roles(self, jwt_service):
        token = jwt_service.generate_access_token(
            user_id="u1", roles=["admin", "developer"],
        )
        payload = jwt_service.verify(token)
        assert "admin" in payload.roles
        assert "developer" in payload.roles

    def test_empty_roles_and_permissions(self, jwt_service):
        token = jwt_service.generate_access_token(user_id="u1")
        payload = jwt_service.verify(token)
        assert payload.roles is None
        assert payload.permissions is None


# ── Password Hashing Tests ────────────────────────────────────────


class TestPasswordHashing:
    """Bcrypt password hashing."""

    def test_hash_password(self):
        from digitorn.core.auth.providers.local import hash_password
        hashed = hash_password("my_password")
        assert isinstance(hashed, str)
        assert hashed.startswith("$2b$")
        assert len(hashed) == 60

    def test_verify_correct_password(self):
        from digitorn.core.auth.providers.local import hash_password, verify_password
        hashed = hash_password("my_password")
        assert verify_password("my_password", hashed) is True

    def test_verify_wrong_password(self):
        from digitorn.core.auth.providers.local import hash_password, verify_password
        hashed = hash_password("my_password")
        assert verify_password("wrong_password", hashed) is False

    def test_different_hashes_for_same_password(self):
        from digitorn.core.auth.providers.local import hash_password
        h1 = hash_password("same_password")
        h2 = hash_password("same_password")
        assert h1 != h2  # bcrypt salt is different each time

    def test_constant_time_verification(self):
        """Verify that bcrypt.checkpw uses constant-time comparison."""
        from digitorn.core.auth.providers.local import hash_password, verify_password
        hashed = hash_password("test")
        # Both should take roughly the same time (bcrypt is constant-time)
        t0 = time.perf_counter()
        verify_password("test", hashed)
        t1 = time.perf_counter()
        verify_password("wrong", hashed)
        t2 = time.perf_counter()
        # Allow 50ms tolerance (bcrypt is slow by design)
        assert abs((t1 - t0) - (t2 - t1)) < 0.05


# ── API Key Tests ─────────────────────────────────────────────────


class TestAPIKey:
    """API key generation."""

    def test_generate_api_key_format(self):
        from digitorn.core.auth.providers.api_key import generate_api_key
        raw, key_hash, prefix = generate_api_key()
        assert raw.startswith("dk_")
        assert prefix.startswith("dk_")
        assert len(key_hash) == 64  # SHA-256

    def test_generate_api_key_uniqueness(self):
        from digitorn.core.auth.providers.api_key import generate_api_key
        keys = [generate_api_key()[0] for _ in range(10)]
        assert len(set(keys)) == 10  # all unique

    def test_api_key_hash_matches(self):
        from digitorn.core.auth.providers.api_key import generate_api_key
        raw, key_hash, _ = generate_api_key()
        computed = hashlib.sha256(raw.encode()).hexdigest()
        assert key_hash == computed

    def test_api_key_prefix_in_raw(self):
        from digitorn.core.auth.providers.api_key import generate_api_key
        raw, _, prefix = generate_api_key()
        assert raw.startswith(prefix)


# ── Middleware Tests ───────────────────────────────────────────────


class TestAuthMiddleware:
    """Auth middleware request processing."""

    def test_public_paths_not_blocked(self):
        from digitorn.core.auth.middleware import _PUBLIC_PATHS
        assert "/auth/login" in _PUBLIC_PATHS
        assert "/auth/register" in _PUBLIC_PATHS
        assert "/health" in _PUBLIC_PATHS

    def test_public_prefixes(self):
        from digitorn.core.auth.middleware import _PUBLIC_PREFIXES
        assert any(p.startswith("/auth/oauth/") for p in _PUBLIC_PREFIXES)


# ── RBAC Tests ────────────────────────────────────────────────────


class TestRBAC:
    """Role-based access control."""

    def test_builtin_roles_defined(self):
        from digitorn.core.auth.service import BUILTIN_ROLES
        assert "admin" in BUILTIN_ROLES
        assert "developer" in BUILTIN_ROLES
        assert "viewer" in BUILTIN_ROLES

    def test_admin_has_wildcard_permission(self):
        from digitorn.core.auth.service import BUILTIN_ROLES
        assert "*" in BUILTIN_ROLES["admin"]["permissions"]

    def test_developer_permissions(self):
        from digitorn.core.auth.service import BUILTIN_ROLES
        perms = BUILTIN_ROLES["developer"]["permissions"]
        assert "apps:read" in perms
        assert "apps:write" in perms
        assert "agents:run" in perms

    def test_viewer_is_read_only(self):
        from digitorn.core.auth.service import BUILTIN_ROLES
        perms = BUILTIN_ROLES["viewer"]["permissions"]
        assert "apps:read" in perms
        assert "apps:write" not in perms
        assert "agents:run" not in perms

    def test_roles_have_descriptions(self):
        from digitorn.core.auth.service import BUILTIN_ROLES
        for name, info in BUILTIN_ROLES.items():
            assert "description" in info
            assert len(info["description"]) > 10


# ── ORM Model Tests ───────────────────────────────────────────────


class TestAuthModels:
    """Auth ORM models structure."""

    def test_role_table_exists(self):
        from digitorn.core.models import Role
        assert Role.__tablename__ == "roles"

    def test_user_role_table_exists(self):
        from digitorn.core.models import UserRole
        assert UserRole.__tablename__ == "user_roles"

    def test_refresh_token_table_exists(self):
        from digitorn.core.models import RefreshToken
        assert RefreshToken.__tablename__ == "refresh_tokens"

    def test_api_key_table_exists(self):
        from digitorn.core.models import APIKey
        assert APIKey.__tablename__ == "api_keys"

    def test_user_has_sessions_relationship(self):
        from digitorn.core.models import User
        assert "sessions" in User.__mapper__.relationships

    def test_user_has_oauth_tokens_relationship(self):
        from digitorn.core.models import User
        assert "oauth_tokens" in User.__mapper__.relationships

    def test_role_has_user_roles_relationship(self):
        from digitorn.core.models import Role
        assert "user_roles" in Role.__mapper__.relationships

    def test_user_role_unique_constraint(self):
        from digitorn.core.models import UserRole
        indexes = {idx.name for idx in UserRole.__table__.indexes}
        assert "ix_user_roles_unique" in indexes

    def test_api_key_prefix_index(self):
        from digitorn.core.models import APIKey
        indexes = {idx.name for idx in APIKey.__table__.indexes}
        assert "ix_api_keys_prefix" in indexes


# ── OAuth2 Provider Tests ─────────────────────────────────────────


class TestOAuth2Provider:
    """OAuth2 provider configuration."""

    def test_well_known_providers(self):
        from digitorn.core.auth.providers.oauth2 import _PROVIDER_CONFIGS
        assert "google" in _PROVIDER_CONFIGS
        assert "github" in _PROVIDER_CONFIGS
        assert "azure" in _PROVIDER_CONFIGS

    def test_google_config_complete(self):
        from digitorn.core.auth.providers.oauth2 import _PROVIDER_CONFIGS
        google = _PROVIDER_CONFIGS["google"]
        assert "authorize_url" in google
        assert "token_url" in google
        assert "userinfo_url" in google
        assert "scopes" in google

    def test_get_authorize_url(self):
        from digitorn.core.auth.providers.oauth2 import OAuth2Provider
        prov = OAuth2Provider()
        prov._authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
        prov._client_id = "test_client"
        prov._redirect_uri = "http://localhost:8000/auth/callback"
        prov._scopes = "openid email"

        url, state = prov.get_authorize_url()
        assert "https://accounts.google.com" in url
        assert "client_id=test_client" in url
        assert "state=" in url
        assert "." in state  # nonce.signature format

    def test_state_is_tracked(self):
        from digitorn.core.auth.providers.oauth2 import OAuth2Provider
        prov = OAuth2Provider()
        prov._authorize_url = "https://example.com/auth"
        prov._client_id = "test"
        prov._redirect_uri = "http://localhost"
        prov._scopes = "openid"

        _, state = prov.get_authorize_url()
        assert state in prov._pending_states


# ── Auth API Router Tests ─────────────────────────────────────────


class TestAuthAPI:
    """Auth API endpoint registration."""

    def test_router_has_9_routes(self):
        from digitorn.core.api.auth import router
        assert len(router.routes) == 9

    def test_login_endpoint_exists(self):
        from digitorn.core.api.auth import router
        paths = [r.path for r in router.routes]
        assert "/auth/login" in paths

    def test_register_endpoint_exists(self):
        from digitorn.core.api.auth import router
        paths = [r.path for r in router.routes]
        assert "/auth/register" in paths

    def test_sessions_endpoint_exists(self):
        from digitorn.core.api.auth import router
        paths = [r.path for r in router.routes]
        assert "/auth/sessions" in paths

    def test_fork_endpoint_exists(self):
        from digitorn.core.api.auth import router
        paths = [r.path for r in router.routes]
        assert "/auth/sessions/{session_id}/fork" in paths

    def test_history_endpoint_exists(self):
        from digitorn.core.api.auth import router
        paths = [r.path for r in router.routes]
        assert "/auth/sessions/{session_id}/history" in paths


# ── JWT Key Auto-Generation Tests ─────────────────────────────────


class TestJWTKeyGeneration:
    """JWT secret key auto-generation."""

    def test_ensure_secret_creates_key(self, tmp_path):
        from digitorn.core.auth.jwt import _ensure_secret
        key_path = tmp_path / "jwt.key"
        key = _ensure_secret(key_path)
        assert len(key) == 128  # 64 bytes hex
        assert key_path.exists()

    def test_ensure_secret_reuses_key(self, tmp_path):
        from digitorn.core.auth.jwt import _ensure_secret
        key_path = tmp_path / "jwt.key"
        key1 = _ensure_secret(key_path)
        key2 = _ensure_secret(key_path)
        assert key1 == key2

    @pytest.mark.skipif(
        __import__("platform").system().lower() == "windows",
        reason="Unix file permissions (0o600) not supported on Windows",
    )
    def test_ensure_secret_file_permissions(self, tmp_path):
        import os
        from digitorn.core.auth.jwt import _ensure_secret
        key_path = tmp_path / "jwt.key"
        _ensure_secret(key_path)
        mode = os.stat(key_path).st_mode & 0o777
        assert mode == 0o600  # owner-only
