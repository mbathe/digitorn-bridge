"""ClientCertificateHandler - mTLS / TLS client certificates."""

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

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Parse the cert + key with cryptography and surface expiry."""
        cert_pem = (fields or {}).get("cert_pem", "")
        key_pem = (fields or {}).get("key_pem", "")
        passphrase = (fields or {}).get("passphrase") or None
        if not cert_pem or not key_pem:
            return False, "cert_pem and key_pem are required"
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa, ec
        except ImportError:
            return True, "PEM markers present (cryptography lib not installed)"
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
        except Exception as exc:
            return False, f"cert parse failed: {exc}"
        try:
            key = serialization.load_pem_private_key(
                key_pem.encode("utf-8"),
                password=passphrase.encode("utf-8") if passphrase else None,
            )
        except Exception as exc:
            return False, f"key parse failed: {exc}"
        # Expiry
        from datetime import datetime, timezone
        try:
            not_after = cert.not_valid_after_utc  # type: ignore[attr-defined]
        except AttributeError:
            not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
        if not_after < datetime.now(timezone.utc):
            return False, f"certificate expired on {not_after.isoformat()}"
        # Key/cert match (public-key bytes equal)
        try:
            cert_pub = cert.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            if isinstance(key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
                key_pub = key.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                if cert_pub != key_pub:
                    return False, "private key does not match the certificate"
        except Exception:
            pass  # best-effort match check
        cn = "(unknown)"
        try:
            from cryptography.x509.oid import NameOID
            cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        except (IndexError, Exception):
            pass
        return True, f"Valid until {not_after.date().isoformat()} (CN={cn})"
