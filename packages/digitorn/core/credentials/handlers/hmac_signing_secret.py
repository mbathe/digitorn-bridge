"""HmacSigningSecretHandler - shared secret for HMAC request signing."""

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

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Compute a sample HMAC and confirm the algorithm + secret work."""
        import hashlib
        import hmac as _hmac
        secret = (fields or {}).get("secret", "")
        if not secret:
            return False, "secret is empty"
        if len(secret) < 8:
            return False, "secret should be at least 8 characters"
        algo = (fields or {}).get("algorithm") or "sha256"
        try:
            digest = _hmac.new(
                secret.encode("utf-8"),
                b"digitorn-test",
                getattr(hashlib, algo),
            ).hexdigest()
        except (AttributeError, ValueError) as exc:
            return False, f"algorithm {algo!r} not supported: {exc}"
        return True, f"HMAC-{algo} OK ({digest[:8]}…)"
