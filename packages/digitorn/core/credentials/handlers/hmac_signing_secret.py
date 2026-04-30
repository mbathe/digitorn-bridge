"""HmacSigningSecretHandler - shared secret for HMAC request signing.

Used by:
  - Webhook receivers that verify incoming requests are signed by the
    expected sender (Stripe, GitHub, Slack delivery signatures).
  - Outgoing webhook senders that need to sign their own requests.
  - Internal service mesh with HMAC-based auth.

The secret is one byte string; the algorithm is declared per-provider
(SHA-256 default, sometimes SHA-1 for legacy).
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler

logger = logging.getLogger(__name__)


class HmacSigningSecretHandler(CredentialHandler):
    provider_type = "hmac_signing_secret"
    allowed_scopes = (
        "system_wide",
        "per_app_shared",
        "per_user",
        "per_app_per_user",
    )

    @classmethod
    def schema_fields(cls) -> list[Any]:
        from digitorn.core.credentials.field_spec import FieldSpec, FieldType
        return [
            FieldSpec(
                name="secret",
                label="Signing secret",
                type=FieldType.PASSWORD,
                required=True,
                masked=True,
                help="Shared secret used to compute / verify HMAC signatures.",
                min_length=8,
                max_length=512,
                inject_path_default="{block}.config.signing_secret",
            ),
            FieldSpec(
                name="algorithm",
                label="Algorithm",
                type=FieldType.SELECT,
                required=False,
                masked=False,
                default="sha256",
                choices=[
                    ("sha256", "HMAC-SHA-256 (recommended)"),
                    ("sha512", "HMAC-SHA-512"),
                    ("sha1", "HMAC-SHA-1 (legacy, GitHub v1, Slack v0)"),
                    ("md5", "HMAC-MD5 (insecure, only for legacy)"),
                ],
                inject_path_default="{block}.config.signing_algorithm",
            ),
            FieldSpec(
                name="header_name",
                label="Header name",
                type=FieldType.TEXT,
                required=False,
                masked=False,
                help="HTTP header where the signature is read/written.",
                placeholder="X-Hub-Signature-256, X-Slack-Signature, ...",
                inject_path_default="{block}.config.signing_header",
            ),
        ]
