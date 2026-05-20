"""DeviceCodeHandler - OAuth 2.0 Device Authorization Grant (RFC 8628)."""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler

logger = logging.getLogger(__name__)


class DeviceCodeHandler(CredentialHandler):
    provider_type = "device_code"
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
                name="device_code",
                label="Device code",
                type=FieldType.PASSWORD,
                required=False,
                masked=True,
                help="Internal - used during polling.",
            ),
        ]
