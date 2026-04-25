"""MultiFieldHandler — credentials that need several correlated fields.

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
    # Everything else inherits from the base: validate_fields already
    # walks the schema_fields list, so multi-field validation comes
    # for free.
