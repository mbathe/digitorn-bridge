"""MCP OAuth2 - provider configuration, authorization flow, token exchange.

Handles the OAuth2 Authorization Code flow for MCP servers that require
user-level authentication (Google Calendar, GitHub user-scope, etc.).

Flow:
1. Agent calls MCP tool → MCPModule detects auth config on server
2. If no token cached → return requires_oauth error with auth_url
3. User opens auth_url → provider redirects to callback with code
4. Callback exchanges code for tokens → stored via UserStore
5. Agent retries → token injected into MCP transport headers

Supports:
- Authorization Code with PKCE (recommended)
- Standard Authorization Code (legacy providers)
- Token refresh via refresh_token grant
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


_WELL_KNOWN_PROVIDERS: dict[str, dict[str, Any]] = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "revoke_url": "https://oauth2.googleapis.com/revoke",
        "default_scopes": [],
        "pkce": True,
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "revoke_url": None,
        "default_scopes": [],
        "pkce": False,
    },
    "slack": {
        "authorize_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "revoke_url": "https://slack.com/api/auth.revoke",
        "default_scopes": [],
        "pkce": False,
    },
    "microsoft": {
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "revoke_url": None,
        "default_scopes": [],
        "pkce": True,
    },
    "notion": {
        "authorize_url": "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
        "revoke_url": None,
        "default_scopes": [],
        "pkce": False,
        "token_auth_method": "basic",
        "extra_authorize_params": {"owner": "user"},
    },
}


@dataclass
class OAuthProviderConfig:
    """OAuth2 provider configuration for an MCP server.

    Parsed from YAML::

        servers:
          google_calendar:
            transport: sse
            url: "http://localhost:3000/sse"
            auth:
              type: oauth2
              provider: google
              client_id: "{{env.GOOGLE_CLIENT_ID}}"
              client_secret: "{{env.GOOGLE_CLIENT_SECRET}}"
              scopes: ["calendar.readonly", "calendar.events"]
              redirect_uri: "http://localhost:8420/api/apps/{app_id}/oauth/callback"
    """

    provider: str
    client_id: str
    client_secret: str
    scopes: list[str] = field(default_factory=list)
    redirect_uri: str = ""
    authorize_url: str = ""
    token_url: str = ""
    revoke_url: str | None = None
    pkce: bool = True
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    token_auth_method: str = "body"
    env_token_var: str = ""

    def __post_init__(self) -> None:
        """Fill in well-known URLs if using a known provider."""
        known = _WELL_KNOWN_PROVIDERS.get(self.provider, {})
        if not self.authorize_url:
            self.authorize_url = known.get("authorize_url", "")
        if not self.token_url:
            self.token_url = known.get("token_url", "")
        if self.revoke_url is None:
            self.revoke_url = known.get("revoke_url")
        if not self.scopes and known.get("default_scopes"):
            self.scopes = known["default_scopes"]
        if "pkce" in known and self.pkce is True:
            self.pkce = known["pkce"]
        if self.token_auth_method == "body" and known.get("token_auth_method"):
            self.token_auth_method = known["token_auth_method"]
        if not self.extra_authorize_params and known.get("extra_authorize_params"):
            self.extra_authorize_params = dict(known["extra_authorize_params"])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthProviderConfig:
        """Parse from YAML auth config block."""
        return cls(
            provider=data.get("provider", "custom"),
            client_id=data.get("client_id", ""),
            client_secret=data.get("client_secret", ""),
            scopes=data.get("scopes", []),
            redirect_uri=data.get("redirect_uri", ""),
            authorize_url=data.get("authorize_url", ""),
            token_url=data.get("token_url", ""),
            revoke_url=data.get("revoke_url"),
            pkce=data.get("pkce", True),
            extra_authorize_params=data.get("extra_params", {}),
            token_auth_method=data.get("token_auth_method", "body"),
            env_token_var=data.get("env_token_var", ""),
        )


@dataclass
class OAuthState:
    """Pending OAuth authorization state (stored in memory)."""

    server_id: str
    provider: str
    user_id: str
    code_verifier: str | None
    redirect_uri: str
    scopes: list[str]
    created_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())

    @property
    def is_expired(self) -> bool:
        """States expire after 10 minutes."""
        age = datetime.now(timezone.utc).timestamp() - self.created_at
        return age > 600


def generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256).

    Returns:
        (code_verifier, code_challenge) - both URL-safe strings.
    """
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OAuthManager:
    """Manages OAuth2 authorization flows for MCP servers.

    One instance per MCPModule (per app). Stores pending auth states
    in memory (ephemeral - they expire after 10 min).
    """

    def __init__(self) -> None:
        self._pending: dict[str, OAuthState] = {}

    def build_authorize_url(
        self,
        config: OAuthProviderConfig,
        server_id: str,
        user_id: str,
    ) -> tuple[str, str]:
        """Build the OAuth authorization URL for a user.

        Args:
            config: Provider config for this MCP server.
            server_id: MCP server ID.
            user_id: Internal user ID.

        Returns:
            (authorize_url, state_key) - redirect user to authorize_url.
        """
        state_key = secrets.token_urlsafe(32)

        params: dict[str, str] = {
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "response_type": "code",
            "state": state_key,
        }

        if config.scopes:
            params["scope"] = " ".join(config.scopes)

        code_verifier: str | None = None
        if config.pkce:
            code_verifier, code_challenge = generate_pkce()
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"

        params.update(config.extra_authorize_params)

        self._pending[state_key] = OAuthState(
            server_id=server_id,
            provider=config.provider,
            user_id=user_id,
            code_verifier=code_verifier,
            redirect_uri=config.redirect_uri,
            scopes=config.scopes,
        )

        url = config.authorize_url + "?" + urlencode(params)
        return url, state_key

    def get_pending_state(self, state_key: str) -> OAuthState | None:
        """Retrieve and remove a pending state (one-time use)."""
        # Opportunistic cleanup of expired states to prevent memory growth
        self._purge_expired()
        state = self._pending.pop(state_key, None)
        if state is not None and state.is_expired:
            logger.warning("oauth_state_expired state=%s", state_key)
            return None
        return state

    def _purge_expired(self) -> None:
        """Remove all expired pending states."""
        expired = [k for k, v in self._pending.items() if v.is_expired]
        for k in expired:
            del self._pending[k]
        if expired:
            logger.debug("oauth_purged_expired count=%d", len(expired))

    async def exchange_code(
        self,
        config: OAuthProviderConfig,
        state: OAuthState,
        authorization_code: str,
    ) -> dict[str, Any]:
        """Exchange authorization code for tokens.

        Args:
            config: Provider config.
            state: The original auth state.
            authorization_code: Code from the callback.

        Returns:
            Token response dict: access_token, refresh_token, expires_in, scope, etc.
        """
        body: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": state.redirect_uri,
        }

        auth = None
        if config.token_auth_method == "basic":
            auth = httpx.BasicAuth(config.client_id, config.client_secret)
        else:
            body["client_id"] = config.client_id
            body["client_secret"] = config.client_secret

        if state.code_verifier:
            body["code_verifier"] = state.code_verifier

        headers = {"Accept": "application/json"}

        try:
            async with httpx.AsyncClient() as client:
                if config.token_auth_method == "basic":
                    headers["Content-Type"] = "application/json"
                    resp = await client.post(
                        config.token_url,
                        json=body,
                        headers=headers,
                        auth=auth,
                        timeout=30.0,
                    )
                else:
                    resp = await client.post(
                        config.token_url,
                        data=body,
                        headers=headers,
                        auth=auth,
                        timeout=30.0,
                    )
                if resp.status_code >= 400:
                    logger.error(
                        "oauth_token_exchange_failed status=%d body=%s "
                        "client_id=%s redirect_uri=%s token_url=%s",
                        resp.status_code,
                        resp.text[:500],
                        config.client_id[:12] + "...",
                        state.redirect_uri,
                        config.token_url,
                    )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise OAuthError(f"Token exchange HTTP error: {exc}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise OAuthError(f"Token exchange returned invalid JSON: {exc}")

        if "error" in data:
            raise OAuthError(
                f"Token exchange failed: {data.get('error_description', data['error'])}"
            )

        logger.info(
            "oauth_token_exchanged provider=%s user=%s",
            config.provider, state.user_id,
        )
        return data

    async def refresh_access_token(
        self,
        config: OAuthProviderConfig,
        refresh_token: str,
    ) -> dict[str, Any]:
        """Refresh an expired access token.

        Args:
            config: Provider config.
            refresh_token: The refresh token.

        Returns:
            Token response dict: access_token, expires_in, etc.
        """
        body: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        auth = None
        if config.token_auth_method == "basic":
            auth = httpx.BasicAuth(config.client_id, config.client_secret)
        else:
            body["client_id"] = config.client_id
            body["client_secret"] = config.client_secret

        headers = {"Accept": "application/json"}

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    config.token_url,
                    data=body,
                    headers=headers,
                    auth=auth,
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise OAuthError(f"Token refresh HTTP error: {exc}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise OAuthError(f"Token refresh returned invalid JSON: {exc}")

        if "error" in data:
            raise OAuthError(
                f"Token refresh failed: {data.get('error_description', data['error'])}"
            )

        logger.info("oauth_token_refreshed provider=%s", config.provider)
        return data

    def cleanup_expired(self) -> int:
        """Remove expired pending states. Returns count removed."""
        expired = [k for k, v in self._pending.items() if v.is_expired]
        for k in expired:
            del self._pending[k]
        return len(expired)


class OAuthError(Exception):
    """Raised when an OAuth operation fails."""


def parse_auth_config(server_config: dict[str, Any]) -> OAuthProviderConfig | None:
    """Parse the auth block from an MCP server YAML config.

    Returns None if no auth is configured.
    """
    auth = server_config.get("auth")
    if not auth or not isinstance(auth, dict):
        return None
    auth_type = auth.get("type", "")
    if auth_type != "oauth2":
        logger.warning("mcp_unknown_auth_type type=%s", auth_type)
        return None
    return OAuthProviderConfig.from_dict(auth)


def parse_token_response(
    data: dict[str, Any],
) -> tuple[str, str | None, datetime | None, str]:
    """Extract standard fields from an OAuth token response.

    Returns:
        (access_token, refresh_token, expires_at, scope)
    """
    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token")
    scope = data.get("scope", "")

    expires_at: datetime | None = None
    if "expires_in" in data:
        try:
            seconds = int(data["expires_in"])
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        except (ValueError, TypeError):
            pass

    return access_token, refresh_token, expires_at, scope
