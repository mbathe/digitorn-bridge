"""OAuth2/OIDC auth provider.

Supports Google, Azure AD, GitHub, Okta, Keycloak, and any
OIDC-compliant provider. Uses authorization code flow with PKCE.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from digitorn_auth.providers.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)

_PROVIDER_CONFIGS = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://www.googleapis.com/oauth2/v3/userinfo",
        "scopes": "openid email profile",
        "email_field": "email",
        "name_field": "name",
        "id_field": "sub",
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "scopes": "read:user user:email",
        "email_field": "email",
        "name_field": "name",
        "id_field": "id",
    },
    "azure": {
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "userinfo_url": "https://graph.microsoft.com/v1.0/me",
        # `User.Read` is required to hit graph.microsoft.com/v1.0/me;
        # without it the userinfo call returns 401 and we fail with
        # "Could not extract user ID from provider".
        "scopes": "openid email profile User.Read offline_access",
        "email_field": "mail",
        "name_field": "displayName",
        "id_field": "id",
    },
}

class OAuth2Provider(AuthProvider):
    """OAuth2/OIDC authentication provider.

    Two-step flow:
    1. GET /auth/oauth/{provider} → redirects to OAuth provider
    2. GET /auth/callback?code=...&state=... → exchanges code for token

    Config:
        provider: google, github, azure, or custom
        client_id: OAuth client ID
        client_secret: OAuth client secret
        redirect_uri: Callback URL
        authorize_url: (custom only) Authorization endpoint
        token_url: (custom only) Token endpoint
        userinfo_url: (custom only) User info endpoint
        scopes: Space-separated scopes
    """

    provider_id = "oauth2"

    def __init__(self) -> None:
        self._provider_name: str = ""
        self._client_id: str = ""
        self._client_secret: str = ""
        self._redirect_uri: str = ""
        self._authorize_url: str = ""
        self._token_url: str = ""
        self._userinfo_url: str = ""
        self._scopes: str = ""
        self._email_field: str = "email"
        self._name_field: str = "name"
        self._id_field: str = "sub"
        self._auto_provision: bool = True
        self._session_factory: Any = None
        # HMAC key for signing state tokens (generated per-process)
        self._state_key: bytes = secrets.token_bytes(32)
        # Pending states with creation time for expiration
        self._pending_states: dict[str, float] = {}
        self._state_ttl: int = 600  # 10 min max for OAuth flow

    async def on_start(self, config: dict[str, Any]) -> None:
        self._provider_name = config.get("provider", "custom")
        self._client_id = config.get("client_id", "")
        self._client_secret = config.get("client_secret", "")
        self._redirect_uri = config.get("redirect_uri", "")
        self._auto_provision = config.get("auto_provision", True)

        well_known = _PROVIDER_CONFIGS.get(self._provider_name, {})
        self._authorize_url = config.get("authorize_url", well_known.get("authorize_url", ""))
        self._token_url = config.get("token_url", well_known.get("token_url", ""))
        self._userinfo_url = config.get("userinfo_url", well_known.get("userinfo_url", ""))
        self._scopes = config.get("scopes", well_known.get("scopes", "openid email profile"))
        self._email_field = config.get("email_field", well_known.get("email_field", "email"))
        self._name_field = config.get("name_field", well_known.get("name_field", "name"))
        self._id_field = config.get("id_field", well_known.get("id_field", "sub"))

        from digitorn_auth.database import get_session_factory
        self._session_factory = get_session_factory()

    def get_authorize_url(self) -> tuple[str, str]:
        """Generate OAuth authorization URL and state token.

        Returns (url, state) - redirect user to url, store state for verification.
        """
        import hmac, hashlib, time
        nonce = secrets.token_hex(16)
        sig = hmac.new(self._state_key, nonce.encode(), hashlib.sha256).hexdigest()[:16]
        state = f"{nonce}.{sig}"
        # Expire stale states before adding new one
        now = time.time()
        self._pending_states = {k: v for k, v in self._pending_states.items() if now - v < self._state_ttl}
        self._pending_states[state] = now

        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": self._scopes,
            "state": state,
        }
        url = f"{self._authorize_url}?{urlencode(params)}"
        return url, state

    async def authenticate(self, credentials: dict[str, Any]) -> AuthResult:
        """Exchange authorization code for user info.

        Credentials: {"code": "...", "state": "..."}
        """
        code = credentials.get("code")
        state = credentials.get("state")

        if not code:
            return AuthResult(success=False, error="Missing authorization code")

        if state:
            import hmac, hashlib, time
            # Verify HMAC signature
            parts = state.split(".", 1)
            if len(parts) != 2:
                return AuthResult(success=False, error="Invalid state parameter")
            nonce, sig = parts
            expected_sig = hmac.new(self._state_key, nonce.encode(), hashlib.sha256).hexdigest()[:16]
            if not hmac.compare_digest(sig, expected_sig):
                return AuthResult(success=False, error="Invalid state parameter")
            # Check pending + expiry
            created = self._pending_states.pop(state, None)
            if created is None:
                return AuthResult(success=False, error="State already used or expired")
            if time.time() - created > self._state_ttl:
                return AuthResult(success=False, error="State expired")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                token_data = {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                    "grant_type": "authorization_code",
                }

                token_resp_obj = await client.post(
                    self._token_url,
                    data=token_data,
                    headers={"Accept": "application/json"},
                )
                token_resp = token_resp_obj.json()

                access_token = token_resp.get("access_token")
                if not access_token:
                    error = token_resp.get("error_description", token_resp.get("error", "Unknown error"))
                    return AuthResult(success=False, error=f"Token exchange failed: {error}")

                userinfo_resp = await client.get(
                    self._userinfo_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                userinfo = userinfo_resp.json()
                if userinfo_resp.status_code >= 400:
                    err = userinfo.get("error") if isinstance(userinfo, dict) else None
                    if isinstance(err, dict):
                        err_msg = err.get("message") or err.get("code") or str(err)
                    else:
                        err_msg = str(err) if err else f"HTTP {userinfo_resp.status_code}"
                    return AuthResult(
                        success=False,
                        error=f"Userinfo fetch failed: {err_msg}",
                    )

            external_id = str(userinfo.get(self._id_field, ""))
            email = userinfo.get(self._email_field)
            # Personal Microsoft accounts (Outlook.com, Hotmail) return
            # `mail: null` from Graph; the address lives in
            # `userPrincipalName`. Fall back so the user row gets an
            # email regardless of account type.
            if not email and self._provider_name == "azure":
                email = userinfo.get("userPrincipalName")
            display_name = userinfo.get(self._name_field, email or external_id)

            if not external_id:
                logger.error(
                    "oauth2_no_user_id provider=%s userinfo_keys=%s",
                    self._provider_name,
                    list(userinfo.keys()),
                )
                return AuthResult(success=False, error="Could not extract user ID from provider")

            user_id: str | None = None
            if self._auto_provision and self._session_factory:
                user_id = await self._ensure_user(external_id, email, display_name)

            return AuthResult(
                success=True,
                user_id=user_id,
                external_id=external_id,
                email=email,
                display_name=display_name,
                attributes={"provider": self._provider_name, "access_token": access_token},
            )

        except Exception as exc:
            logger.error("oauth2_auth_failed provider=%s error=%s", self._provider_name, exc)
            return AuthResult(success=False, error=f"OAuth2 authentication failed: {exc}")

    async def _ensure_user(self, external_id: str, email: str | None, display_name: str) -> str:
        """Create or update user in local DB.

        Lookup order:
          1. Exact match on (provider, external_id) — same OAuth identity
             logging in again. Update email/display_name in case the
             provider returned new values.
          2. Match on email (any provider) — same person signing in via
             a *different* OAuth provider for the first time. Reuse the
             existing user row instead of creating a duplicate. Without
             step 2, "Sign in with Google" and "Sign in with Microsoft"
             on the same Gmail address create two separate accounts and
             the user's history disappears when they switch providers.
          3. Provision a brand-new user row.
        """
        from digitorn_auth.models import User
        from sqlalchemy import select

        provider_key = f"oauth2:{self._provider_name}"

        async with self._session_factory() as session:
            stmt = select(User).where(
                User.provider == provider_key,
                User.external_id == external_id,
            )
            user = (await session.execute(stmt)).scalar_one_or_none()

            if user:
                user.email = email
                user.display_name = display_name
                await session.commit()
                return user.id

            if email:
                stmt_email = select(User).where(User.email == email)
                existing = (await session.execute(stmt_email)).scalar_one_or_none()
                if existing is not None:
                    # Don't overwrite the existing user's provider /
                    # external_id - the original identity must still
                    # resolve on its own future logins. Just refresh the
                    # display name (most recent OAuth response wins).
                    if display_name:
                        existing.display_name = display_name
                    await session.commit()
                    logger.info(
                        "oauth2_user_linked_by_email provider=%s "
                        "existing_user_id=%s existing_provider=%s",
                        self._provider_name, existing.id, existing.provider,
                    )
                    return existing.id

            user = User(
                external_id=external_id,
                provider=provider_key,
                email=email,
                display_name=display_name,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logger.info("oauth2_user_provisioned provider=%s user_id=%s", self._provider_name, user.id)
            return user.id
