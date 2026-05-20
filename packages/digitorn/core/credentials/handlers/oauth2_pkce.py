"""OAuth2PkceHandler - OAuth 2.0 with PKCE (no client_secret)."""

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
