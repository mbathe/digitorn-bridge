"""SshKeyHandler - SSH private key + optional passphrase."""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler, ValidationError

logger = logging.getLogger(__name__)


class SshKeyHandler(CredentialHandler):
    provider_type = "ssh_key"
    # SSH keys are typically per-user (each developer their own
    # identity) but a CI key can be system_wide or per_app_shared.
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
                name="private_key",
                label="Private key (PEM)",
                type=FieldType.TEXTAREA,
                required=True,
                masked=True,
                help="Paste the full private key including BEGIN/END headers.",
                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----",
                inject_path_default="{block}.config.ssh_private_key_path",
            ),
            FieldSpec(
                name="passphrase",
                label="Passphrase",
                type=FieldType.PASSWORD,
                required=False,
                masked=True,
                help="Required only if the key is encrypted at rest.",
            ),
            FieldSpec(
                name="public_key",
                label="Public key",
                type=FieldType.TEXTAREA,
                required=False,
                masked=False,
                help="Optional - extracted from the private key if not provided.",
                placeholder="ssh-ed25519 AAAA... user@host",
            ),
            FieldSpec(
                name="known_hosts_entry",
                label="Known hosts entry",
                type=FieldType.TEXTAREA,
                required=False,
                masked=False,
                help="Optional - host fingerprint for strict checking.",
            ),
        ]

    def validate_fields(
        self,
        fields: dict[str, Any],
        schema_fields: list[dict[str, Any]],
    ) -> None:
        super().validate_fields(fields, schema_fields)
        priv = (fields or {}).get("private_key", "")
        if not priv:
            return
        # Light structural check; we don't load paramiko for validation
        # to avoid a heavy dependency on the pure-Python validation path.
        priv_stripped = priv.strip()
        valid_starts = (
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "-----BEGIN RSA PRIVATE KEY-----",
            "-----BEGIN EC PRIVATE KEY-----",
            "-----BEGIN DSA PRIVATE KEY-----",
            "-----BEGIN PRIVATE KEY-----",
        )
        if not any(priv_stripped.startswith(s) for s in valid_starts):
            raise ValidationError(
                "private_key",
                "does not look like a PEM-formatted SSH private key "
                "(expected one of the BEGIN ... PRIVATE KEY headers)",
            )

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Try to load the key with paramiko - validates format +"""
        priv = (fields or {}).get("private_key", "")
        passphrase = (fields or {}).get("passphrase") or None
        if not priv:
            return False, "private_key required"

        try:
            import io
            import paramiko
        except ImportError:
            return True, None  # paramiko optional - trust validation

        try:
            # Try each known key type until one parses.
            errs = []
            for key_cls in (
                paramiko.Ed25519Key,
                paramiko.ECDSAKey,
                paramiko.RSAKey,
                paramiko.DSSKey,
            ):
                try:
                    key_cls.from_private_key(io.StringIO(priv), password=passphrase)
                    return True, None
                except Exception as e:
                    errs.append(f"{key_cls.__name__}: {e}")
            return False, "key did not parse as any known type: " + "; ".join(errs[:2])
        except Exception as exc:
            return False, str(exc)
