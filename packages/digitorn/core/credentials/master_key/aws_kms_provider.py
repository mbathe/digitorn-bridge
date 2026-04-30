"""AwsKmsProvider - envelope encryption backed by AWS KMS.

Two operational layers:

1. **Master key**: a CMK (Customer Master Key) in AWS KMS, identified
   by ARN or alias. NEVER leaves AWS. IAM controls who can call
   `Encrypt` / `Decrypt`. Auditable via CloudTrail.

2. **Per-record DEK**: the daemon calls `GenerateDataKey` to get a
   fresh 32-byte AES key for each credential. AWS returns both the
   plaintext DEK (used immediately to encrypt) AND a ciphertext blob
   (the wrapped DEK encrypted with the CMK). The daemon stores ONLY
   the ciphertext blob alongside the credential row. The plaintext
   DEK is zeroized after use.

On read: the daemon hands the wrapped DEK back to KMS via `Decrypt`,
gets the plaintext DEK, decrypts the credential, zeroizes the DEK.

Configuration via env vars (Digitorn convention):

    DIGITORN_KMS=aws_kms
    AWS_KMS_KEY_ID=arn:aws:kms:eu-west-1:123:key/...   # required
    AWS_REGION=eu-west-1                                # required if not in env
    AWS_PROFILE=digitorn                                # optional
    # IAM credentials via standard chain (env / instance profile / SSO)

Required IAM permissions:
    kms:GenerateDataKey
    kms:Decrypt
    kms:DescribeKey   (for healthcheck)

Failure modes the daemon handles:
  - KMS unreachable → healthcheck returns False; new credential ops fail
    cleanly. Existing credentials decrypted via cached DEKs (unwrap is
    REQUIRED on read so a network blip = no reads either).
  - DEK wrap rejected (rotated CMK, policy change) → UnwrapFailed,
    surfaced as "credential decryption failed - contact admin".

Note: this module imports `boto3` lazily so the daemon can run without
the AWS SDK installed (env / file providers don't need it).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from digitorn.core.credentials.master_key.provider import (
    KeyMaterial,
    KmsBackend,
    UnwrapFailed,
)

logger = logging.getLogger(__name__)


class AwsKmsProvider:
    """Implements `MasterKeyProvider` via AWS KMS envelope encryption."""

    def __init__(
        self,
        key_id: str | None = None,
        region: str | None = None,
        profile: str | None = None,
    ) -> None:
        self._key_id = key_id or os.environ.get("AWS_KMS_KEY_ID", "")
        if not self._key_id:
            raise ValueError(
                "AWS_KMS_KEY_ID env var or `key_id` arg required for "
                "AwsKmsProvider",
            )
        self._region = region or os.environ.get("AWS_REGION", "")
        self._profile = profile or os.environ.get("AWS_PROFILE")
        self._client: Any = None
        logger.info(
            "master_key_provider=aws_kms key_id=%s region=%s profile=%s",
            self._key_id, self._region or "<default>",
            self._profile or "<default>",
        )

    def _ensure_client(self) -> Any:
        """Lazy-create the boto3 KMS client. Imported here so the AWS
        SDK is only required when this provider is actually used."""
        if self._client is not None:
            return self._client
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "boto3 is required for AwsKmsProvider. "
                "Install with `pip install boto3` and restart the daemon.",
            ) from exc
        session_kwargs: dict[str, Any] = {}
        if self._profile:
            session_kwargs["profile_name"] = self._profile
        session = boto3.Session(**session_kwargs)
        client_kwargs: dict[str, Any] = {}
        if self._region:
            client_kwargs["region_name"] = self._region
        self._client = session.client("kms", **client_kwargs)
        return self._client

    @property
    def backend(self) -> KmsBackend:
        return KmsBackend.AWS_KMS

    @property
    def wraps_data_key(self) -> bool:
        return True  # envelope mode

    @property
    def key_id(self) -> str | None:
        return self._key_id

    async def get_data_key(self) -> KeyMaterial:
        """Call KMS GenerateDataKey to mint a fresh AES-256 DEK.

        Returns the plaintext DEK (use immediately, zeroize after) AND
        the wrapped form (persist alongside ciphertext)."""
        import asyncio
        client = self._ensure_client()

        def _call() -> dict[str, Any]:
            return client.generate_data_key(
                KeyId=self._key_id,
                KeySpec="AES_256",
            )

        try:
            resp = await asyncio.to_thread(_call)
        except Exception as exc:
            logger.error("aws_kms_generate_data_key_failed: %s", exc)
            raise

        return KeyMaterial(
            data_key=resp["Plaintext"],
            wrapped_dek=resp["CiphertextBlob"],
            backend=KmsBackend.AWS_KMS,
            key_id=self._key_id,
        )

    async def unwrap_data_key(self, wrapped: bytes) -> KeyMaterial:
        """Call KMS Decrypt to recover the plaintext DEK from its
        wrapped form. Used at credential read time."""
        import asyncio
        client = self._ensure_client()

        def _call() -> dict[str, Any]:
            return client.decrypt(CiphertextBlob=wrapped)

        try:
            resp = await asyncio.to_thread(_call)
        except Exception as exc:
            logger.error("aws_kms_decrypt_failed: %s", exc)
            raise UnwrapFailed(f"AWS KMS decrypt rejected the wrapped DEK: {exc}") from exc

        return KeyMaterial(
            data_key=resp["Plaintext"],
            wrapped_dek=wrapped,
            backend=KmsBackend.AWS_KMS,
            key_id=resp.get("KeyId", self._key_id),
        )

    async def healthcheck(self) -> bool:
        """Probe KMS via DescribeKey - cheap, doesn't actually generate
        or decrypt anything but verifies IAM + reachability."""
        import asyncio
        try:
            client = self._ensure_client()

            def _call() -> dict[str, Any]:
                return client.describe_key(KeyId=self._key_id)

            await asyncio.to_thread(_call)
            return True
        except Exception as exc:
            logger.warning("aws_kms_healthcheck_failed: %s", exc)
            return False

    async def close(self) -> None:
        # boto3 clients hold connection pools; closing is best-effort.
        try:
            if self._client is not None and hasattr(self._client, "close"):
                self._client.close()
        except Exception:
            pass
        self._client = None
