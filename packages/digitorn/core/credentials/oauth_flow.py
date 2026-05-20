"""OAuth 2.0 authorization code flow runner."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from digitorn.core.credentials.oauth_providers import OAuthProviderConfig
from digitorn.core.credentials.store import CredentialStore, Scope, Status

logger = logging.getLogger(__name__)


FLOW_TTL_SECONDS = 600  # 10 minutes - generous for the user to consent


# Pending flow tracking


@dataclass
class PendingFlow:
    """One in-progress OAuth flow waiting for the user to consent."""

    state: str
    provider_name: str
    provider: OAuthProviderConfig
    user_id: str
    app_id: str
    scopes: list[str]
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | connected | error
    error: str | None = None
    resulting_credential_id: str | None = None
    target_name: str | None = None
    target_scope: str | None = None

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.created_at > FLOW_TTL_SECONDS


class PendingFlowStore:
    """In-memory store for active OAuth flows."""

    def __init__(self) -> None:
        self._flows: dict[str, PendingFlow] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        provider: OAuthProviderConfig,
        user_id: str,
        app_id: str,
        scopes: list[str] | None = None,
    ) -> PendingFlow:
        """Create a new pending flow and return it."""
        if not provider.is_configured():
            raise RuntimeError(
                f"OAuth provider {provider.name!r} has no client_id or client_secret. "
                f"Configure it in ~/.digitorn/oauth_providers.toml."
            )

        state = secrets.token_urlsafe(32)
        effective_scopes = scopes or provider.default_scopes
        flow = PendingFlow(
            state=state,
            provider_name=provider.name,
            provider=provider,
            user_id=user_id,
            app_id=app_id,
            scopes=list(effective_scopes),
        )
        async with self._lock:
            self._sweep_expired_locked()
            self._flows[state] = flow
        return flow

    async def get(self, state: str) -> PendingFlow | None:
        async with self._lock:
            flow = self._flows.get(state)
            if flow and flow.expired():
                del self._flows[state]
                return None
            return flow

    async def mark_connected(
        self, state: str, credential_id: str,
    ) -> None:
        async with self._lock:
            flow = self._flows.get(state)
            if flow is not None:
                flow.status = "connected"
                flow.resulting_credential_id = credential_id

    async def mark_error(self, state: str, error: str) -> None:
        async with self._lock:
            flow = self._flows.get(state)
            if flow is not None:
                flow.status = "error"
                flow.error = error

    async def forget(self, state: str) -> None:
        async with self._lock:
            self._flows.pop(state, None)

    def _sweep_expired_locked(self) -> None:
        now = time.time()
        stale = [s for s, f in self._flows.items() if f.expired(now)]
        for s in stale:
            del self._flows[s]


# Module-level singleton - created on first use by the route layer.
default_flow_store: PendingFlowStore | None = None


def get_default_flow_store() -> PendingFlowStore:
    global default_flow_store
    if default_flow_store is None:
        default_flow_store = PendingFlowStore()
    return default_flow_store


# Authorization URL construction


def build_auth_url(flow: PendingFlow) -> str:
    """Build the URL the user's browser opens to consent."""
    params: dict[str, str] = {
        "client_id": flow.provider.client_id,
        "redirect_uri": flow.provider.redirect_uri,
        "response_type": "code",
        "state": flow.state,
    }
    if flow.scopes:
        params["scope"] = " ".join(flow.scopes)
    params.update(flow.provider.extra_auth_params or {})
    sep = "&" if "?" in flow.provider.auth_url else "?"
    return f"{flow.provider.auth_url}{sep}{urlencode(params)}"


# Token exchange (authorization_code + refresh_token)


class TokenExchangeError(Exception):
    """Raised when the provider rejects the code/refresh exchange."""


class TokenExchange:
    """Stateless helpers for talking to the provider's /token endpoint."""

    @staticmethod
    async def exchange_code(
        provider: OAuthProviderConfig,
        code: str,
    ) -> dict[str, Any]:
        """Exchange an authorization code for an access token."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": provider.redirect_uri,
        }
        return await TokenExchange._post(provider, data)

    @staticmethod
    async def refresh_token(
        provider: OAuthProviderConfig,
        refresh_token: str,
    ) -> dict[str, Any]:
        """Exchange a refresh token for a new access token."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        return await TokenExchange._post(provider, data)

    @staticmethod
    async def _post(
        provider: OAuthProviderConfig,
        data: dict[str, str],
    ) -> dict[str, Any]:
        try:
            import aiohttp
        except ImportError as exc:
            raise TokenExchangeError(
                "aiohttp not available - OAuth token exchange requires it"
            ) from exc

        auth = None
        if provider.auth_style == "basic":
            auth = aiohttp.BasicAuth(
                provider.client_id, provider.client_secret,
            )
        else:  # "post"
            data = {
                **data,
                "client_id": provider.client_id,
                "client_secret": provider.client_secret,
            }

        # GitHub wants Accept: application/json or it returns
        # application/x-www-form-urlencoded by default. Safe to always send.
        headers = {"Accept": "application/json"}

        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    provider.token_url,
                    data=data,
                    auth=auth,
                    headers=headers,
                ) as r:
                    try:
                        body = await r.json(content_type=None)
                    except Exception:
                        text = await r.text()
                        raise TokenExchangeError(
                            f"non-JSON response from {provider.name}: "
                            f"HTTP {r.status} - {text[:200]}"
                        )
                    if r.status != 200:
                        err = (
                            body.get("error_description")
                            or body.get("error")
                            or body.get("message")
                            or f"HTTP {r.status}"
                        )
                        raise TokenExchangeError(
                            f"token exchange failed for {provider.name}: {err}"
                        )
                    return body
        except aiohttp.ClientError as exc:
            raise TokenExchangeError(
                f"network error talking to {provider.name}: {exc}"
            ) from exc


# Credential writer - glue between token exchange and the store


async def persist_oauth_credential(
    *,
    store: CredentialStore,
    flow: PendingFlow,
    token_response: dict[str, Any],
) -> dict[str, Any]:
    """Write the result of a successful token exchange into the store."""
    fields: dict[str, Any] = {
        "access_token": token_response["access_token"],
    }
    refresh_token = token_response.get("refresh_token")
    if refresh_token:
        fields["refresh_token"] = refresh_token
    token_type = token_response.get("token_type")
    if token_type:
        fields["token_type"] = token_type
    scope = token_response.get("scope")
    if scope:
        fields["scope"] = scope

    # Expiry
    expires_at = None
    expires_in = token_response.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        from datetime import datetime, timedelta, timezone
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    # Display metadata - NOT encrypted, shown in the UI
    display_metadata: dict[str, Any] = {
        "oauth_provider": flow.provider_name,
        "oauth_scopes": (
            scope.split() if isinstance(scope, str) else flow.scopes
        ),
    }

    for key in ("bot_user_id", "authed_user", "team", "workspace_name", "owner"):
        val = token_response.get(key)
        if val:
            display_metadata[key] = val if isinstance(val, (str, int)) else str(val)

    stored = await store.upsert_user_credential(
        user_id=flow.user_id,
        provider_name=flow.provider_name,
        provider_type="oauth2",
        label="default",
        fields=fields,
        status=Status.VALID,
        expires_at=expires_at,
        display_metadata=display_metadata,
        name=flow.target_name,
        scope=flow.target_scope,
    )
    try:
        await store.create_grant(
            credential_id=stored["id"],
            user_id=flow.user_id,
            app_id=flow.app_id,
            scopes_granted=display_metadata.get("oauth_scopes") or [],
        )
    except Exception as exc:
        logger.warning(
            "persist_oauth_credential: grant creation failed for "
            "user=%s app=%s cred=%s: %s",
            flow.user_id, flow.app_id, stored.get("id"), exc,
        )
    return stored
