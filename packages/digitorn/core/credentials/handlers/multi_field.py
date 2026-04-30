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
