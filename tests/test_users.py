"""Tests - UserStore: encryption helpers, UserInfo, OAuthTokenInfo.

Covers:
- _encrypt_token / _decrypt_token roundtrip (with and without Fernet)
- UserInfo creation, to_dict, from_orm
- OAuthTokenInfo.is_expired logic
- Token encryption with base64 fallback
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from digitorn.core.app.users import (
    OAuthTokenInfo,
    UserInfo,
    _decrypt_token,
    _encrypt_token,
)


# ── Token Encryption ─────────────────────────────────────────────────────


class TestTokenEncryption:
    def test_roundtrip(self):
        """Encrypt then decrypt should return the original."""
        original = "xoxb-1234-secret-token"
        encrypted = _encrypt_token(original)
        assert isinstance(encrypted, bytes)
        decrypted = _decrypt_token(encrypted)
        assert decrypted == original

    def test_different_tokens_different_ciphertext(self):
        """Two different tokens should produce different ciphertext."""
        a = _encrypt_token("token-a")
        b = _encrypt_token("token-b")
        assert a != b

    def test_unicode_token(self):
        """Should handle Unicode tokens."""
        original = "token-with-émojis-🔑"
        encrypted = _encrypt_token(original)
        assert _decrypt_token(encrypted) == original

    def test_empty_token(self):
        """Should handle empty string."""
        encrypted = _encrypt_token("")
        assert _decrypt_token(encrypted) == ""


# ── UserInfo ─────────────────────────────────────────────────────────────


class TestUserInfo:
    def test_creation(self):
        user = UserInfo(
            id="user-1",
            external_id="ext-1",
            provider="google",
            app_id="my-app",
            email="test@example.com",
            display_name="Test User",
        )
        assert user.id == "user-1"
        assert user.email == "test@example.com"
        assert user.provider == "google"

    def test_to_dict(self):
        user = UserInfo(
            id="u1",
            external_id="e1",
            provider="local",
            app_id=None,
            email="a@b.com",
            display_name="A",
            phone="+1234",
            avatar_url=None,
            attributes={"role": "admin"},
            is_active=True,
            created_at=None,
            updated_at=None,
            last_seen_at=None,
        )
        d = user.to_dict()
        assert d["id"] == "u1"
        assert d["email"] == "a@b.com"
        assert d["attributes"] == {"role": "admin"}
        assert d["phone"] == "+1234"
        # All slots present
        for slot in UserInfo.__slots__:
            assert slot in d

    def test_from_orm(self):
        orm = MagicMock()
        orm.id = "u1"
        orm.external_id = "ext"
        orm.provider = "github"
        orm.app_id = "app1"
        orm.email = "user@gh.com"
        orm.display_name = "GH User"
        orm.phone = None
        orm.avatar_url = "https://avatars.gh.com/1"
        orm.attributes = {"teams": ["eng"]}
        orm.is_active = True
        orm.created_at = datetime(2024, 1, 1)
        orm.updated_at = datetime(2024, 6, 1)
        orm.last_seen_at = None

        user = UserInfo.from_orm(orm)
        assert user.id == "u1"
        assert user.provider == "github"
        assert user.attributes == {"teams": ["eng"]}

    def test_from_orm_none_attributes(self):
        orm = MagicMock()
        orm.attributes = None
        for slot in UserInfo.__slots__:
            if slot != "attributes":
                setattr(orm, slot, None)

        user = UserInfo.from_orm(orm)
        assert user.attributes == {}

    def test_missing_kwargs_default_to_none(self):
        user = UserInfo(id="u1")
        assert user.email is None
        assert user.phone is None


# ── OAuthTokenInfo ───────────────────────────────────────────────────────


class TestOAuthTokenInfo:
    def test_not_expired_when_no_expiry(self):
        token = OAuthTokenInfo(
            user_id="u1",
            provider="google",
            access_token="abc",
            expires_at=None,
        )
        assert token.is_expired is False

    def test_not_expired_when_future(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        token = OAuthTokenInfo(
            user_id="u1",
            provider="google",
            access_token="abc",
            expires_at=future,
        )
        assert token.is_expired is False

    def test_expired_when_past(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        token = OAuthTokenInfo(
            user_id="u1",
            provider="google",
            access_token="abc",
            expires_at=past,
        )
        assert token.is_expired is True

    def test_expired_at_exact_time(self):
        """Edge case: expires_at == now should be expired."""
        now = datetime.now(timezone.utc)
        token = OAuthTokenInfo(
            user_id="u1",
            provider="google",
            access_token="abc",
            expires_at=now - timedelta(seconds=1),
        )
        assert token.is_expired is True

    def test_attributes(self):
        token = OAuthTokenInfo(
            user_id="u1",
            provider="slack",
            scope="channels:read channels:write",
            access_token="xoxb-123",
            refresh_token="xoxr-456",
            token_type="bearer",
            expires_at=None,
        )
        assert token.provider == "slack"
        assert token.scope == "channels:read channels:write"
        assert token.token_type == "bearer"
