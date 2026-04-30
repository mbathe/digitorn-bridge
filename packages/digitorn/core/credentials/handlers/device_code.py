"""DeviceCodeHandler - OAuth 2.0 Device Authorization Grant (RFC 8628).

Designed for input-constrained devices (TVs, CLIs, IoT) where the
browser flow happens on a SECONDARY device. The daemon:

  1. Calls `/device/code` on the provider, gets `device_code` +
     `user_code` + `verification_uri`.
  2. Shows the `user_code` + `verification_uri` to the user.
  3. Polls `/token` with `device_code` until the user has logged in
     on their phone/laptop and approved.
  4. Receives `access_token` + `refresh_token`, stores via the store.

Real-world consumers: GitHub CLI (`gh auth login`), Anthropic Claude
Code CLI, AWS IAM Identity Center sign-in.

Storage shape is identical to OAuth2 - what differs is only the
acquisition flow.
"""

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
