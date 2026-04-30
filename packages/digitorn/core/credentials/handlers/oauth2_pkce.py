"""OAuth2PkceHandler - OAuth 2.0 with PKCE (no client_secret).

Used by public clients (mobile apps, single-page apps, CLIs running
on user machines) that cannot safely store a client_secret. PKCE
(Proof Key for Code Exchange, RFC 7636) protects the authorization
code with a per-flow code_verifier so a stolen code can't be
exchanged for a token without the verifier.

Common consumers: Auth0 SPA apps, Okta consumer apps, AWS Cognito
hosted UI in mobile flows, OAuth providers that don't require the
secret to be stored client-side.

Same field shape as OAuth2 (access_token + refresh_token + ...) but
the token-exchange step uses a `code_verifier` instead of a
`client_secret`. The handler is otherwise identical to OAuth2 once
the tokens are stored.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler

logger = logging.getLogger(__name__)


class OAuth2PkceHandler(CredentialHandler):
    provider_type = "oauth2_pkce"
    # PKCE tokens are personal - same per-user constraint as OAuth2.
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
            ),
            FieldSpec(
                name="refresh_token",
                label="Refresh token",
                type=FieldType.PASSWORD,
                required=False,
                masked=True,
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
            ),
            FieldSpec(
                name="code_verifier",
                label="Code verifier",
                type=FieldType.PASSWORD,
                required=False,
                masked=True,
                help="Internal - used during the PKCE exchange.",
            ),
        ]
