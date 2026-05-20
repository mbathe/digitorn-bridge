"""OAuth2Handler - 3-legged OAuth with refresh token support."""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.handler import (
    CredentialHandler,
    HandlerNotAvailable,
    now_utc,
)

logger = logging.getLogger(__name__)


class OAuth2Handler(CredentialHandler):
    provider_type = "oauth2"
    allowed_scopes = ("per_user", "per_app_per_user")

    @classmethod
    def schema_fields(cls) -> list[Any]:
        from digitorn.core.credentials.field_spec import FieldSpec, FieldType
        return [
            FieldSpec(
                name="access_token",
                label="Access token",
                type=FieldType.PASSWORD,
                required=True,
                masked=True,
                help="Issued by the OAuth provider after the user authorises the app.",
            ),
            FieldSpec(
                name="refresh_token",
                label="Refresh token",
                type=FieldType.PASSWORD,
                required=False,
                masked=True,
                help="Used to renew the access token before expiry.",
            ),
            FieldSpec(
                name="token_type",
                label="Token type",
                type=FieldType.TEXT,
                required=False,
                default="Bearer",
            ),
            FieldSpec(
                name="scope",
                label="Granted scopes",
                type=FieldType.TEXT,
                required=False,
                help="Space-separated list of scopes actually granted.",
            ),
        ]

    def validate_fields(
        self,
        fields: dict[str, Any],
        schema_fields: list[dict[str, Any]],
    ) -> None:
        """OAuth tokens come from the provider exchange, not user input."""
        if not fields:
            raise ValueError("OAuth credential has no fields")
        if "access_token" not in fields:
            raise ValueError("OAuth credential missing access_token")

    async def refresh(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> dict[str, Any]:
        """Exchange the refresh token for a new access token."""
        fields = credential.get("fields") or {}
        refresh_token = fields.get("refresh_token")
        if not refresh_token:
            updated = dict(credential)
            updated["status"] = "invalid"
            updated["last_error"] = "no refresh_token, reconnect required"
            return updated

        from digitorn.core.credentials.oauth_flow import (
            TokenExchange,
            TokenExchangeError,
        )
        from digitorn.core.credentials.oauth_providers import (
            get_default_registry,
        )

        provider_name = schema_provider.get("oauth_provider") or credential.get(
            "provider_name"
        )
        provider = get_default_registry().get(provider_name)
        if provider is None or not provider.is_configured():
            raise HandlerNotAvailable(
                f"OAuth provider {provider_name!r} is not configured "
                f"in ~/.digitorn/oauth_providers.toml - cannot refresh."
            )

        try:
            token_response = await TokenExchange.refresh_token(
                provider, refresh_token,
            )
        except TokenExchangeError as exc:
            updated = dict(credential)
            updated["status"] = "invalid"
            updated["last_error"] = f"refresh failed: {exc}"
            return updated

        # Build the new fields dict. Some providers return a new
        # refresh_token, others don't - keep the old one when absent.
        new_fields = dict(fields)
        new_fields["access_token"] = token_response["access_token"]
        if token_response.get("refresh_token"):
            new_fields["refresh_token"] = token_response["refresh_token"]
        if token_response.get("token_type"):
            new_fields["token_type"] = token_response["token_type"]
        if token_response.get("scope"):
            new_fields["scope"] = token_response["scope"]

        expires_at = None
        expires_in = token_response.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            from datetime import datetime, timedelta, timezone
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            ).isoformat()

        updated = dict(credential)
        updated["fields"] = new_fields
        updated["status"] = "valid"
        updated["expires_at"] = expires_at
        updated["last_validated_at"] = now_utc().isoformat()
        updated["last_error"] = None
        return updated

    async def revoke(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> None:
        """POST the provider's revocation endpoint."""
        from digitorn.core.credentials.oauth_providers import (
            get_default_registry,
        )
        provider_name = credential.get("provider_name") or ""
        registry = get_default_registry()
        provider_cfg = registry.get(provider_name)
        if provider_cfg is None or not provider_cfg.revoke_url:
            # No revocation endpoint declared - silent ok.
            logger.info(
                "oauth_revoke_skip provider=%s (no revoke_url configured)",
                provider_name,
            )
            return

        fields = credential.get("fields") or {}
        token = (
            fields.get("access_token")
            or fields.get("token")
            or fields.get("refresh_token")
            or ""
        )
        if not token:
            logger.info(
                "oauth_revoke_skip provider=%s (no token to revoke)",
                provider_name,
            )
            return

        try:
            import aiohttp
        except ImportError:
            raise HandlerNotAvailable(
                "aiohttp is required for OAuth revocation but is not installed.",
            )

        try:
            async with aiohttp.ClientSession() as session:
                # Most providers accept `?token=X` as a query parameter
                # or POST body. We send both for max compatibility.
                async with session.post(
                    provider_cfg.revoke_url,
                    data={"token": token},
                    auth=(
                        aiohttp.BasicAuth(
                            provider_cfg.client_id,
                            provider_cfg.client_secret,
                        )
                        if provider_cfg.auth_style == "basic"
                        else None
                    ),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status >= 400:
                        body = (await resp.text())[:200]
                        logger.warning(
                            "oauth_revoke_failed provider=%s status=%s body=%s",
                            provider_name, resp.status, body,
                        )
                    else:
                        logger.info(
                            "oauth_revoked provider=%s cred=%s",
                            provider_name, credential.get("id"),
                        )
        except Exception as exc:
            logger.warning(
                "oauth_revoke_error provider=%s: %s",
                provider_name, exc,
            )
