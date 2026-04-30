"""AzureKeyVaultProvider - envelope encryption via Azure Key Vault.

Same envelope pattern. The CMK is a Key in an Azure Key Vault, accessed
via Azure AD authentication (managed identity, service principal, or
Azure CLI).

Configuration:

    DIGITORN_KMS=azure_kv
    AZURE_KV_VAULT_URL=https://digitorn-kv.vault.azure.net
    AZURE_KV_KEY_NAME=digitorn-master
    AZURE_KV_KEY_VERSION=  (optional; defaults to current)
    # Auth via DefaultAzureCredential chain:
    #   - AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET
    #   - or managed identity if running on Azure VM/AKS

The Azure Key Vault Encrypt/Decrypt API works with `RSA-OAEP-256` for
asymmetric keys or `AES256GCM` / `AES-KW` for symmetric. We use
`RSA-OAEP-256` by default (most widely supported) but the algorithm is
configurable via env var if you need symmetric.
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
DEFAULT_ALG = "RSA-OAEP-256"


class AzureKeyVaultProvider:
    """Implements `MasterKeyProvider` via Azure Key Vault."""

    def __init__(
        self,
        vault_url: str | None = None,
        key_name: str | None = None,
        key_version: str | None = None,
        algorithm: str | None = None,
    ) -> None:
        self._vault_url = vault_url or os.environ.get("AZURE_KV_VAULT_URL", "")
        self._key_name = key_name or os.environ.get("AZURE_KV_KEY_NAME", "")
        self._key_version = key_version or os.environ.get("AZURE_KV_KEY_VERSION") or None
        self._alg = algorithm or os.environ.get("AZURE_KV_ALGORITHM", DEFAULT_ALG)
        if not self._vault_url or not self._key_name:
            raise ValueError(
                "AZURE_KV_VAULT_URL and AZURE_KV_KEY_NAME env vars required "
                "for AzureKeyVaultProvider",
            )
        self._client: Any = None
        logger.info(
            "master_key_provider=azure_kv vault=%s key=%s version=%s alg=%s",
            self._vault_url, self._key_name,
            self._key_version or "<latest>", self._alg,
        )

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from azure.keyvault.keys.crypto import CryptographyClient
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise RuntimeError(
                "azure-identity and azure-keyvault-keys are required for "
                "AzureKeyVaultProvider. Install with "
                "`pip install azure-identity azure-keyvault-keys`.",
            ) from exc

        credential = DefaultAzureCredential()
        # Build the key identifier - with or without version pin.
        key_id = f"{self._vault_url.rstrip('/')}/keys/{self._key_name}"
        if self._key_version:
            key_id = f"{key_id}/{self._key_version}"
        self._client = CryptographyClient(key_id, credential=credential)
        return self._client

    @property
    def backend(self) -> KmsBackend:
        return KmsBackend.AZURE_KV

    @property
    def wraps_data_key(self) -> bool:
        return True

    @property
    def key_id(self) -> str | None:
        return f"{self._vault_url}/keys/{self._key_name}/{self._key_version or 'latest'}"

    async def get_data_key(self) -> KeyMaterial:
        import asyncio
        dek = _secrets.token_bytes(KEY_LEN)
        client = self._ensure_client()
        from azure.keyvault.keys.crypto import EncryptionAlgorithm

        def _call() -> Any:
            alg = EncryptionAlgorithm(self._alg)
            return client.encrypt(alg, dek)

        try:
            resp = await asyncio.to_thread(_call)
        except Exception as exc:
            logger.error("azure_kv_encrypt_failed: %s", exc)
            raise

        return KeyMaterial(
            data_key=dek,
            wrapped_dek=resp.ciphertext,
            backend=KmsBackend.AZURE_KV,
            key_id=self.key_id,
        )

    async def unwrap_data_key(self, wrapped: bytes) -> KeyMaterial:
        import asyncio
        client = self._ensure_client()
        from azure.keyvault.keys.crypto import EncryptionAlgorithm

        def _call() -> Any:
            alg = EncryptionAlgorithm(self._alg)
            return client.decrypt(alg, wrapped)

        try:
            resp = await asyncio.to_thread(_call)
        except Exception as exc:
            logger.error("azure_kv_decrypt_failed: %s", exc)
            raise UnwrapFailed(
                f"Azure Key Vault decrypt rejected the wrapped DEK: {exc}"
            ) from exc

        return KeyMaterial(
            data_key=resp.plaintext,
            wrapped_dek=wrapped,
            backend=KmsBackend.AZURE_KV,
            key_id=self.key_id,
        )

    async def healthcheck(self) -> bool:
        import asyncio
        try:
            client = self._ensure_client()
            # CryptographyClient doesn't have a cheap probe; the
            # cheapest thing is to fetch the key metadata via
            # `get_key_operations()` (which the SDK calls lazily on
            # first use). Trigger by trying to access `.key`.
            def _call() -> Any:
                return client.key

            key = await asyncio.to_thread(_call)
            return key is not None
        except Exception as exc:
            logger.warning("azure_kv_healthcheck_failed: %s", exc)
            return False

    async def close(self) -> None:
        try:
            if self._client is not None and hasattr(self._client, "close"):
                self._client.close()
        except Exception:
            pass
        self._client = None
