"""Tests — MCP OAuth2: provider config, PKCE, authorize flow, token exchange, refresh.

Covers:
- OAuthProviderConfig from_dict + well-known providers
- PKCE generation (code_verifier + S256 challenge)
- OAuthManager.build_authorize_url() with/without PKCE
- OAuthState expiration
- OAuthManager.get_pending_state() one-time use + expiry
- OAuthManager.exchange_code() with mock HTTP
- OAuthManager.refresh_access_token() with mock HTTP
- parse_auth_config() from server YAML
- parse_token_response() standard fields
- UserStore.refresh_token_if_needed() with callback
"""
from __future__ import annotations

import hashlib
import time
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.modules.mcp.oauth import (
    OAuthError,
    OAuthManager,
    OAuthProviderConfig,
    OAuthState,
    generate_pkce,
    parse_auth_config,
    parse_token_response,
)


# ── OAuthProviderConfig ──────────────────────────────────────────────────


class TestOAuthProviderConfig:
    def test_from_dict_google(self):
        config = OAuthProviderConfig.from_dict({
            "provider": "google",
            "client_id": "test-id",
            "client_secret": "test-secret",
            "scopes": ["calendar.readonly"],
        })
        assert config.provider == "google"
        assert config.client_id == "test-id"
        assert "accounts.google.com" in config.authorize_url
        assert "googleapis.com" in config.token_url
        assert config.pkce is True

    def test_from_dict_github(self):
        config = OAuthProviderConfig.from_dict({
            "provider": "github",
            "client_id": "gh-id",
            "client_secret": "gh-secret",
        })
        assert "github.com" in config.authorize_url
        assert config.pkce is False  # GitHub doesn't support PKCE

    def test_from_dict_custom(self):
        config = OAuthProviderConfig.from_dict({
            "provider": "custom",
            "client_id": "id",
            "client_secret": "secret",
            "authorize_url": "https://auth.example.com/authorize",
            "token_url": "https://auth.example.com/token",
            "scopes": ["read", "write"],
            "pkce": True,
        })
        assert config.authorize_url == "https://auth.example.com/authorize"
        assert config.token_url == "https://auth.example.com/token"
        assert config.scopes == ["read", "write"]

    def test_from_dict_defaults(self):
        config = OAuthProviderConfig.from_dict({})
        assert config.provider == "custom"
        assert config.client_id == ""
        assert config.scopes == []

    def test_from_dict_slack(self):
        config = OAuthProviderConfig.from_dict({
            "provider": "slack",
            "client_id": "sl-id",
            "client_secret": "sl-secret",
        })
        assert "slack.com" in config.authorize_url

    def test_from_dict_microsoft(self):
        config = OAuthProviderConfig.from_dict({
            "provider": "microsoft",
            "client_id": "ms-id",
            "client_secret": "ms-secret",
        })
        assert "microsoftonline.com" in config.authorize_url
        assert config.pkce is True

    def test_extra_authorize_params(self):
        config = OAuthProviderConfig.from_dict({
            "provider": "google",
            "client_id": "id",
            "client_secret": "secret",
            "extra_params": {"access_type": "offline", "prompt": "consent"},
        })
        assert config.extra_authorize_params == {
            "access_type": "offline",
            "prompt": "consent",
        }

    def test_explicit_redirect_uri(self):
        config = OAuthProviderConfig.from_dict({
            "provider": "google",
            "client_id": "id",
            "client_secret": "secret",
            "redirect_uri": "https://myapp.com/callback",
        })
        assert config.redirect_uri == "https://myapp.com/callback"


# ── PKCE ─────────────────────────────────────────────────────────────────


class TestPKCE:
    def test_generate_pkce(self):
        verifier, challenge = generate_pkce()
        assert len(verifier) > 40
        assert len(challenge) > 20
        # Verify S256: challenge == base64url(sha256(verifier))
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert challenge == expected

    def test_generate_pkce_unique(self):
        """Each call should produce unique values."""
        v1, c1 = generate_pkce()
        v2, c2 = generate_pkce()
        assert v1 != v2
        assert c1 != c2


# ── OAuthState ───────────────────────────────────────────────────────────


class TestOAuthState:
    def test_not_expired(self):
        state = OAuthState(
            server_id="s",
            provider="google",
            user_id="u1",
            code_verifier=None,
            redirect_uri="http://localhost/cb",
            scopes=["read"],
        )
        assert state.is_expired is False

    def test_expired(self):
        state = OAuthState(
            server_id="s",
            provider="google",
            user_id="u1",
            code_verifier=None,
            redirect_uri="http://localhost/cb",
            scopes=[],
            created_at=time.time() - 700,  # 11+ minutes ago
        )
        assert state.is_expired is True


# ── OAuthManager ─────────────────────────────────────────────────────────


class TestOAuthManagerBuildUrl:
    def test_build_authorize_url_with_pkce(self):
        manager = OAuthManager()
        config = OAuthProviderConfig(
            provider="google",
            client_id="test-id",
            client_secret="test-secret",
            scopes=["calendar.readonly"],
            redirect_uri="http://localhost:8420/cb",
            pkce=True,
        )
        url, state_key = manager.build_authorize_url(config, "gcal", "user1")

        assert "client_id=test-id" in url
        assert "redirect_uri=" in url
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert "state=" in url
        assert "scope=calendar.readonly" in url
        assert len(state_key) > 10

        # State should be stored
        stored = manager._pending.get(state_key)
        assert stored is not None
        assert stored.server_id == "gcal"
        assert stored.user_id == "user1"
        assert stored.code_verifier is not None

    def test_build_authorize_url_without_pkce(self):
        manager = OAuthManager()
        config = OAuthProviderConfig(
            provider="github",
            client_id="gh-id",
            client_secret="gh-secret",
            redirect_uri="http://localhost/cb",
            pkce=False,
        )
        url, state_key = manager.build_authorize_url(config, "gh", "user2")

        assert "code_challenge" not in url
        stored = manager._pending.get(state_key)
        assert stored.code_verifier is None

    def test_build_authorize_url_extra_params(self):
        manager = OAuthManager()
        config = OAuthProviderConfig(
            provider="google",
            client_id="id",
            client_secret="secret",
            redirect_uri="http://localhost/cb",
            extra_authorize_params={"access_type": "offline"},
        )
        url, _ = manager.build_authorize_url(config, "s", "u")
        assert "access_type=offline" in url


class TestOAuthManagerState:
    def test_get_pending_state_one_time(self):
        manager = OAuthManager()
        config = OAuthProviderConfig(
            provider="google",
            client_id="id",
            client_secret="secret",
            redirect_uri="http://localhost/cb",
        )
        _, state_key = manager.build_authorize_url(config, "s", "u")

        # First retrieval succeeds
        state = manager.get_pending_state(state_key)
        assert state is not None
        assert state.server_id == "s"

        # Second retrieval returns None (one-time use)
        assert manager.get_pending_state(state_key) is None

    def test_get_pending_state_expired(self):
        manager = OAuthManager()
        manager._pending["old"] = OAuthState(
            server_id="s",
            provider="google",
            user_id="u",
            code_verifier=None,
            redirect_uri="http://localhost/cb",
            scopes=[],
            created_at=time.time() - 700,
        )
        assert manager.get_pending_state("old") is None

    def test_get_pending_state_unknown(self):
        manager = OAuthManager()
        assert manager.get_pending_state("nonexistent") is None

    def test_cleanup_expired(self):
        manager = OAuthManager()
        manager._pending["fresh"] = OAuthState(
            server_id="s", provider="p", user_id="u",
            code_verifier=None, redirect_uri="", scopes=[],
        )
        manager._pending["old"] = OAuthState(
            server_id="s", provider="p", user_id="u",
            code_verifier=None, redirect_uri="", scopes=[],
            created_at=time.time() - 700,
        )
        removed = manager.cleanup_expired()
        assert removed == 1
        assert "fresh" in manager._pending
        assert "old" not in manager._pending


class TestOAuthManagerExchange:
    @pytest.mark.asyncio
    async def test_exchange_code_success(self):
        manager = OAuthManager()
        config = OAuthProviderConfig(
            provider="google",
            client_id="id",
            client_secret="secret",
            redirect_uri="http://localhost/cb",
        )
        state = OAuthState(
            server_id="gcal",
            provider="google",
            user_id="u1",
            code_verifier="test-verifier",
            redirect_uri="http://localhost/cb",
            scopes=["calendar.readonly"],
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "ya29.access",
            "refresh_token": "1//refresh",
            "expires_in": 3600,
            "scope": "calendar.readonly",
            "token_type": "Bearer",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("digitorn.modules.mcp.oauth.httpx.AsyncClient", return_value=mock_client):
            data = await manager.exchange_code(config, state, "auth-code-123")

        assert data["access_token"] == "ya29.access"
        assert data["refresh_token"] == "1//refresh"
        # Verify code_verifier was sent
        call_kwargs = mock_client.post.call_args
        assert call_kwargs[1]["data"]["code_verifier"] == "test-verifier"

    @pytest.mark.asyncio
    async def test_exchange_code_error_response(self):
        manager = OAuthManager()
        config = OAuthProviderConfig(
            provider="google",
            client_id="id",
            client_secret="secret",
        )
        state = OAuthState(
            server_id="s", provider="google", user_id="u",
            code_verifier=None, redirect_uri="http://localhost/cb", scopes=[],
        )

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = '{"error":"invalid_grant","error_description":"Code has expired"}'
        mock_response.json.return_value = {
            "error": "invalid_grant",
            "error_description": "Code has expired",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("digitorn.modules.mcp.oauth.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(OAuthError, match="Code has expired"):
                await manager.exchange_code(config, state, "expired-code")


class TestOAuthManagerRefresh:
    @pytest.mark.asyncio
    async def test_refresh_success(self):
        manager = OAuthManager()
        config = OAuthProviderConfig(
            provider="google",
            client_id="id",
            client_secret="secret",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "ya29.new",
            "expires_in": 3600,
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("digitorn.modules.mcp.oauth.httpx.AsyncClient", return_value=mock_client):
            data = await manager.refresh_access_token(config, "1//old-refresh")

        assert data["access_token"] == "ya29.new"

    @pytest.mark.asyncio
    async def test_refresh_error(self):
        manager = OAuthManager()
        config = OAuthProviderConfig(
            provider="google",
            client_id="id",
            client_secret="secret",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "error": "invalid_grant",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("digitorn.modules.mcp.oauth.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(OAuthError, match="invalid_grant"):
                await manager.refresh_access_token(config, "bad-refresh")


# ── parse_auth_config ────────────────────────────────────────────────────


class TestParseAuthConfig:
    def test_with_oauth2(self):
        config = parse_auth_config({
            "auth": {
                "type": "oauth2",
                "provider": "google",
                "client_id": "id",
                "client_secret": "secret",
                "scopes": ["calendar.readonly"],
            }
        })
        assert config is not None
        assert config.provider == "google"
        assert config.client_id == "id"

    def test_no_auth(self):
        assert parse_auth_config({}) is None
        assert parse_auth_config({"auth": None}) is None

    def test_unknown_type(self):
        assert parse_auth_config({"auth": {"type": "saml"}}) is None

    def test_not_dict_auth(self):
        assert parse_auth_config({"auth": "invalid"}) is None


# ── parse_token_response ─────────────────────────────────────────────────


class TestParseTokenResponse:
    def test_full_response(self):
        access, refresh, expires_at, scope = parse_token_response({
            "access_token": "ya29.test",
            "refresh_token": "1//refresh",
            "expires_in": 3600,
            "scope": "calendar.readonly email",
        })
        assert access == "ya29.test"
        assert refresh == "1//refresh"
        assert expires_at is not None
        assert expires_at > datetime.now(timezone.utc)
        assert scope == "calendar.readonly email"

    def test_minimal_response(self):
        access, refresh, expires_at, scope = parse_token_response({
            "access_token": "token",
        })
        assert access == "token"
        assert refresh is None
        assert expires_at is None
        assert scope == ""

    def test_invalid_expires_in(self):
        _, _, expires_at, _ = parse_token_response({
            "access_token": "t",
            "expires_in": "not-a-number",
        })
        assert expires_at is None

    def test_empty_response(self):
        access, refresh, expires_at, scope = parse_token_response({})
        assert access == ""
        assert refresh is None


# ── UserStore.refresh_token_if_needed ────────────────────────────────────


class TestRefreshTokenIfNeeded:
    @pytest.mark.asyncio
    async def test_no_token_returns_none(self):
        from digitorn.core.app.users import UserStore

        store = UserStore()
        store.get_token = AsyncMock(return_value=None)

        result = await store.refresh_token_if_needed("u1", "google")
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_token_returned_as_is(self):
        from digitorn.core.app.users import OAuthTokenInfo, UserStore

        store = UserStore()
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        token = OAuthTokenInfo(
            user_id="u1", provider="google",
            access_token="valid", refresh_token="r",
            token_type="bearer", scope="read",
            expires_at=future,
        )
        store.get_token = AsyncMock(return_value=token)

        result = await store.refresh_token_if_needed("u1", "google")
        assert result is not None
        assert result.access_token == "valid"

    @pytest.mark.asyncio
    async def test_expired_token_refreshed(self):
        from digitorn.core.app.users import OAuthTokenInfo, UserStore

        store = UserStore()
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        expired_token = OAuthTokenInfo(
            user_id="u1", provider="google",
            access_token="old", refresh_token="refresh-token",
            token_type="bearer", scope="read",
            expires_at=past,
        )
        refreshed_token = OAuthTokenInfo(
            user_id="u1", provider="google",
            access_token="new-access", refresh_token="refresh-token",
            token_type="bearer", scope="read",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        # First call returns expired, second returns refreshed
        store.get_token = AsyncMock(side_effect=[expired_token, refreshed_token])
        store.store_token = AsyncMock()

        callback = AsyncMock(return_value={
            "access_token": "new-access",
            "expires_in": 3600,
        })

        result = await store.refresh_token_if_needed(
            "u1", "google", refresh_callback=callback,
        )
        assert result is not None
        assert result.access_token == "new-access"
        callback.assert_awaited_once_with("refresh-token")
        store.store_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_expired_no_callback_returns_none(self):
        from digitorn.core.app.users import OAuthTokenInfo, UserStore

        store = UserStore()
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        token = OAuthTokenInfo(
            user_id="u1", provider="google",
            access_token="old", refresh_token="r",
            token_type="bearer", scope="",
            expires_at=past,
        )
        store.get_token = AsyncMock(return_value=token)

        result = await store.refresh_token_if_needed("u1", "google")
        assert result is None

    @pytest.mark.asyncio
    async def test_expired_no_refresh_token_returns_none(self):
        from digitorn.core.app.users import OAuthTokenInfo, UserStore

        store = UserStore()
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        token = OAuthTokenInfo(
            user_id="u1", provider="google",
            access_token="old", refresh_token=None,
            token_type="bearer", scope="",
            expires_at=past,
        )
        store.get_token = AsyncMock(return_value=token)

        callback = AsyncMock()
        result = await store.refresh_token_if_needed(
            "u1", "google", refresh_callback=callback,
        )
        assert result is None
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refresh_callback_failure_returns_none(self):
        from digitorn.core.app.users import OAuthTokenInfo, UserStore

        store = UserStore()
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        token = OAuthTokenInfo(
            user_id="u1", provider="google",
            access_token="old", refresh_token="r",
            token_type="bearer", scope="",
            expires_at=past,
        )
        store.get_token = AsyncMock(return_value=token)

        callback = AsyncMock(side_effect=Exception("network error"))
        result = await store.refresh_token_if_needed(
            "u1", "google", refresh_callback=callback,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_expiry_returns_token(self):
        from digitorn.core.app.users import OAuthTokenInfo, UserStore

        store = UserStore()
        token = OAuthTokenInfo(
            user_id="u1", provider="google",
            access_token="forever", refresh_token=None,
            token_type="bearer", scope="",
            expires_at=None,
        )
        store.get_token = AsyncMock(return_value=token)

        result = await store.refresh_token_if_needed("u1", "google")
        assert result is not None
        assert result.access_token == "forever"


# ── MCPModule OAuth integration ──────────────────────────────────────────


class TestMCPModuleOAuthIntegration:
    @pytest.mark.asyncio
    async def test_execute_mcp_tool_no_auth_config(self):
        """Tool call on a server without auth should work normally."""
        from digitorn.modules.mcp.module import MCPModule
        from digitorn.modules.mcp.protocol import MCPToolResult

        mcp = MCPModule()
        mcp._server_sandbox = {"slack": set()}
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "ok"}], is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)
        mcp._pool.get_server = MagicMock(return_value=MagicMock(auth_config=None))

        result = await mcp._execute_mcp_tool("slack", "post_message", {"text": "hi"})
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_mcp_tool_requires_oauth(self):
        """Tool call on OAuth server without token should return auth_url."""
        from digitorn.modules.base import ExecutionContext
        from digitorn.modules.mcp.module import MCPModule

        mcp = MCPModule()
        mcp._server_sandbox = {"gcal": set()}
        mcp._user_store = MagicMock()
        mcp._user_store.resolve_user_for_session = AsyncMock(
            return_value=MagicMock(id="user-1"),
        )
        mcp._user_store.refresh_token_if_needed = AsyncMock(return_value=None)

        auth_config = OAuthProviderConfig(
            provider="google",
            client_id="id",
            client_secret="secret",
            redirect_uri="http://localhost/cb",
        )
        entry = MagicMock()
        entry.auth_config = auth_config
        mcp._pool.get_server = MagicMock(return_value=entry)

        ctx = ExecutionContext(
            plan_id="test", action_id="test", session_id="sess-1",
        )

        result = await mcp._execute_mcp_tool("gcal", "list_events", {}, ctx)
        assert result.success is False
        assert result.metadata["requires_oauth"] is True
        assert "auth_url" in result.metadata
        assert "accounts.google.com" in result.metadata["auth_url"]

    @pytest.mark.asyncio
    async def test_execute_mcp_tool_with_valid_token(self):
        """Tool call with valid cached token should proceed normally."""
        from digitorn.core.app.users import OAuthTokenInfo
        from digitorn.modules.base import ExecutionContext
        from digitorn.modules.mcp.module import MCPModule
        from digitorn.modules.mcp.protocol import MCPToolResult

        mcp = MCPModule()
        mcp._server_sandbox = {"gcal": set()}

        token = OAuthTokenInfo(
            user_id="u1", provider="google",
            access_token="ya29.valid", refresh_token="r",
            token_type="Bearer", scope="calendar",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        mcp._user_store = MagicMock()
        mcp._user_store.resolve_user_for_session = AsyncMock(
            return_value=MagicMock(id="u1"),
        )
        mcp._user_store.refresh_token_if_needed = AsyncMock(return_value=token)

        auth_config = OAuthProviderConfig(
            provider="google",
            client_id="id",
            client_secret="secret",
            redirect_uri="http://localhost/cb",
        )

        transport = MagicMock()
        transport._headers = {}
        entry = MagicMock()
        entry.auth_config = auth_config
        entry.transport = transport
        entry._connect_kwargs = {}
        entry.tools = []
        mcp._pool.get_server = MagicMock(return_value=entry)
        mcp._pool.reconnect = AsyncMock()

        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "events"}], is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        ctx = ExecutionContext(
            plan_id="test", action_id="test", session_id="sess-1",
        )

        result = await mcp._execute_mcp_tool("gcal", "list_events", {}, ctx)
        assert result.success is True
        # Token should have been injected into transport headers
        assert transport._headers["Authorization"] == "Bearer ya29.valid"

    @pytest.mark.asyncio
    async def test_execute_mcp_tool_no_user_store(self):
        """Without user store, OAuth server attempts local flow and returns error on failure."""
        from unittest.mock import patch, AsyncMock
        from digitorn.modules.mcp.module import MCPModule

        mcp = MCPModule()
        mcp._server_sandbox = {"s": set()}
        mcp._user_store = None

        auth_config = OAuthProviderConfig(
            provider="google",
            client_id="id",
            client_secret="secret",
        )
        entry = MagicMock()
        entry.auth_config = auth_config
        entry._connect_kwargs = {}
        mcp._pool.get_server = MagicMock(return_value=entry)

        # Mock the local OAuth flow to simulate failure
        with patch(
            "digitorn.modules.mcp.module.run_local_oauth_flow",
            new_callable=AsyncMock,
            side_effect=RuntimeError("OAuth flow failed"),
            create=True,
        ), patch(
            "digitorn.modules.mcp.local_oauth.run_local_oauth_flow",
            new_callable=AsyncMock,
            side_effect=RuntimeError("OAuth flow failed"),
            create=True,
        ):
            result = await mcp._execute_mcp_tool("s", "tool", {})
        assert result.success is False
        assert result.metadata["requires_oauth"] is True
        assert "OAuth flow failed" in result.error
