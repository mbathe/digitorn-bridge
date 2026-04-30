"""ClientCertificateHandler - mTLS / TLS client certificates.

Stores a PEM-formatted certificate + private key (+ optional CA chain
+ passphrase). At runtime, the resolver writes them to temp files in
the session's tmpdir (mode 0600) and exposes paths to the consumer
module via injection.

Common consumers:
  - Internal corporate APIs requiring mTLS.
  - Smart-card-equivalent client auth.
  - Banking / fintech B2B integrations.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler, ValidationError

logger = logging.getLogger(__name__)


_PEM_BEGIN_CERT = "-----BEGIN CERTIFICATE-----"
_PEM_BEGIN_PRIV = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
)


class ClientCertificateHandler(CredentialHandler):
    provider_type = "client_certificate"
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
                name="cert_pem",
                label="Client certificate (PEM)",
                type=FieldType.TEXTAREA,
                required=True,
                masked=False,
                help="Public certificate. Paste the full PEM block.",
                placeholder="-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----",
                inject_path_default="{block}.config.cert_path",
            ),
            FieldSpec(
                name="key_pem",
                label="Private key (PEM)",
                type=FieldType.TEXTAREA,
                required=True,
                masked=True,
                help="Matching private key.",
                placeholder="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----",
                inject_path_default="{block}.config.key_path",
            ),
            FieldSpec(
                name="ca_pem",
                label="CA chain (PEM)",
                type=FieldType.TEXTAREA,
                required=False,
                masked=False,
                help="Optional - intermediate / root CA certificates.",
                inject_path_default="{block}.config.ca_path",
            ),
            FieldSpec(
                name="passphrase",
                label="Key passphrase",
                type=FieldType.PASSWORD,
                required=False,
                masked=True,
                help="Required only if the private key is encrypted.",
            ),
        ]

    def validate_fields(
        self,
        fields: dict[str, Any],
        schema_fields: list[dict[str, Any]],
    ) -> None:
        super().validate_fields(fields, schema_fields)
        cert = (fields or {}).get("cert_pem", "")
        key = (fields or {}).get("key_pem", "")
        if cert and _PEM_BEGIN_CERT not in cert:
            raise ValidationError(
                "cert_pem", "missing BEGIN CERTIFICATE marker",
            )
        if key and not any(b in key for b in _PEM_BEGIN_PRIV):
            raise ValidationError(
                "key_pem",
                "does not look like a PEM private key",
            )
