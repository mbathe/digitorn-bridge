"""GcpKmsProvider - envelope encryption via Google Cloud KMS.

Same envelope pattern as AwsKmsProvider, but the wrap/unwrap calls go
to Google Cloud KMS via the Cloud KMS API. The CMK lives in a Google
Cloud key ring; the daemon's service account has `cloudkms.cryptoKeyVersions.useToEncrypt`
and `useToDecrypt` permissions.

Configuration:

    DIGITORN_KMS=gcp_kms
    GCP_KMS_KEY_NAME=projects/PRJ/locations/LOC/keyRings/RING/cryptoKeys/KEY
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json   # standard ADC

The GCP KMS CryptoKey purpose must be `ENCRYPT_DECRYPT` (the
default). For HSM-backed keys, choose protection level
`HSM` at key creation time - this provider doesn't care, the API is
identical.

Note: GCP KMS Encrypt/Decrypt take arbitrary plaintext (not data
keys), so we hand it the actual DEK bytes for wrap and the
returned ciphertext IS the wrapped DEK. No GenerateDataKey-equivalent
is needed (we use cryptography.hazmat to generate the DEK locally).
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
from typing import Any

from digitorn.core.credentials.master_key.provider import (
    KeyMaterial,
    KmsBackend,
    UnwrapFailed,
)

logger = logging.getLogger(__name__)


KEY_LEN = 32


class GcpKmsProvider:
    """Implements `MasterKeyProvider` via Google Cloud KMS."""

    def __init__(self, key_name: str | None = None) -> None:
        self._key_name = key_name or os.environ.get("GCP_KMS_KEY_NAME", "")
        if not self._key_name:
            raise ValueError(
                "GCP_KMS_KEY_NAME env var or `key_name` arg required for "
                "GcpKmsProvider. Format: "
                "projects/PRJ/locations/LOC/keyRings/RING/cryptoKeys/KEY",
            )
        self._client: Any = None
        logger.info(
            "master_key_provider=gcp_kms key_name=%s",
            self._key_name,
        )

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google.cloud import kms
        except ImportError as exc:
            raise RuntimeError(
                "google-cloud-kms is required for GcpKmsProvider. "
                "Install with `pip install google-cloud-kms` and restart "
                "the daemon.",
            ) from exc
        self._client = kms.KeyManagementServiceClient()
        return self._client

    @property
    def backend(self) -> KmsBackend:
        return KmsBackend.GCP_KMS

    @property
    def wraps_data_key(self) -> bool:
        return True

    @property
    def key_id(self) -> str | None:
        return self._key_name

    async def get_data_key(self) -> KeyMaterial:
        """Generate a fresh DEK locally then wrap it via GCP KMS Encrypt."""
        import asyncio
        dek = _secrets.token_bytes(KEY_LEN)
        client = self._ensure_client()

        def _call() -> Any:
            return client.encrypt(
                request={"name": self._key_name, "plaintext": dek},
            )

        try:
            resp = await asyncio.to_thread(_call)
        except Exception as exc:
            logger.error("gcp_kms_encrypt_failed: %s", exc)
            raise

        wrapped = resp.ciphertext
        return KeyMaterial(
            data_key=dek,
            wrapped_dek=wrapped,
            backend=KmsBackend.GCP_KMS,
            key_id=self._key_name,
        )

    async def unwrap_data_key(self, wrapped: bytes) -> KeyMaterial:
        """Call GCP KMS Decrypt to recover the plaintext DEK."""
        import asyncio
        client = self._ensure_client()

        def _call() -> Any:
            return client.decrypt(
                request={"name": self._key_name, "ciphertext": wrapped},
            )

        try:
            resp = await asyncio.to_thread(_call)
        except Exception as exc:
            logger.error("gcp_kms_decrypt_failed: %s", exc)
            raise UnwrapFailed(
                f"GCP KMS decrypt rejected the wrapped DEK: {exc}"
            ) from exc

        return KeyMaterial(
            data_key=resp.plaintext,
            wrapped_dek=wrapped,
            backend=KmsBackend.GCP_KMS,
            key_id=self._key_name,
        )

    async def healthcheck(self) -> bool:
        import asyncio
        try:
            client = self._ensure_client()

            def _call() -> Any:
                return client.get_crypto_key(request={"name": self._key_name})

            await asyncio.to_thread(_call)
            return True
        except Exception as exc:
            logger.warning("gcp_kms_healthcheck_failed: %s", exc)
            return False

    async def close(self) -> None:
        try:
            if self._client is not None and hasattr(self._client, "transport"):
                # The KMS client uses gRPC; closing the transport
                # releases the channel.
                t = self._client.transport
                if hasattr(t, "close"):
                    t.close()
        except Exception:
            pass
        self._client = None
