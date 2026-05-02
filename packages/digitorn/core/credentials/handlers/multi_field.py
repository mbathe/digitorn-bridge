"""MultiFieldHandler - credentials that need several correlated fields.

Examples: Slack (bot_token + signing_secret + app_token), Twilio
(account_sid + auth_token), AWS (access_key_id + secret_access_key
+ region), Stripe (publishable + secret).

Behaviour is identical to ``ApiKeyHandler`` except the validation
accepts multiple required fields. The handler is mostly a marker
type so the Flutter client knows to render a **multi-field form**
with each field labeled and validated separately.
"""

from __future__ import annotations

from typing import Any

from digitorn.core.credentials.handler import CredentialHandler


class MultiFieldHandler(CredentialHandler):
    provider_type = "multi_field"
    # Multi-field credentials (Slack bot, Twilio, Stripe, ...) can sit
    # at any scope: a corporate Twilio account can be system_wide, a
    # personal Stripe key per_user.
    allowed_scopes = (
        "system_wide",
        "per_app_shared",
        "per_user",
        "per_app_per_user",
    )
    # `schema_fields()` intentionally returns an empty list: the
    # provider catalog declares fields per-provider (e.g. Stripe lists
    # publishable_key + secret_key + webhook_signing_secret).
    # The base validate_fields() walks whatever the catalog gave it.

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Run the catalogue's `test` recipe if any, fall back to a
        generic base_url ping otherwise.

        The schema can declare the same `test` block as api_key.
        Without one we look for a `base_url` field in the credential
        and a likely auth field (token / api_key / secret_key /
        bot_token) and ping with `Authorization: Bearer <value>`.
        Lets users verify Slack / Stripe / Twilio creds before save.
        """
        f = fields or {}
        # 1. Catalogue path: TOML ships an explicit test recipe
        test = schema_provider.get("test") if schema_provider else None
        if test:
            from digitorn.core.credentials.handlers.api_key import (
                ApiKeyHandler,
            )
            return await ApiKeyHandler().test_live_connection(f, schema_provider)
        # 2. Custom path: pick a plausible auth field + base_url
        base_url = str(f.get("base_url") or f.get("api_url") or "").strip()
        if not base_url:
            # No URL to ping. As long as required fields are non-empty,
            # we report success (the user is told it's a soft check).
            for k, v in f.items():
                if v == "":
                    return False, f"field {k!r} is empty"
            return True, "All fields set (no base_url to ping)"
        token = ""
        for k in ("token", "api_key", "secret_key", "bot_token", "access_token",
                  "auth_token"):
            if f.get(k):
                token = str(f[k])
                break
        if not token:
            return True, f"Reachable check skipped (no obvious auth field)"
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as cl:
                resp = await cl.get(
                    base_url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code in (401, 403):
                    return False, f"Auth rejected (HTTP {resp.status_code})"
                return True, f"Reachable (HTTP {resp.status_code})"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
