"""AwsKmsProvider - envelope encryption backed by AWS KMS."""

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
        """Lazy-create the boto3 KMS client. Imported here so the AWS"""
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
        """Call KMS GenerateDataKey to mint a fresh AES-256 DEK."""
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
        """Call KMS Decrypt to recover the plaintext DEK from its"""
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
        """Probe KMS via DescribeKey - cheap, doesn't actually generate"""
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
        except Exception as exc:
            logger.debug("aws_kms_provider best-effort block failed: %s", exc)
        self._client = None
