"""VaultTransitProvider - envelope encryption via HashiCorp Vault Transit.

Vault's Transit secrets engine acts as encryption-as-a-service: the
key material lives in Vault, never leaves. The daemon authenticates
to Vault (token, AppRole, K8s, etc.), then calls
`POST /transit/encrypt/{key}` to wrap a DEK and `POST /transit/decrypt/{key}`
to unwrap.

Configuration:

    DIGITORN_KMS=vault
    VAULT_ADDR=https://vault.internal:8200
    VAULT_TOKEN=hvs.XXXXX                       # OR
    VAULT_ROLE_ID + VAULT_SECRET_ID             # AppRole
    VAULT_TRANSIT_KEY=digitorn-master           # name of the transit key
    VAULT_TRANSIT_MOUNT=transit                 # mount point (default: transit)
    VAULT_NAMESPACE=                            # Vault Enterprise namespace

Required Vault policy:

    path "transit/encrypt/digitorn-master" { capabilities = ["update"] }
    path "transit/decrypt/digitorn-master" { capabilities = ["update"] }
    path "transit/keys/digitorn-master"    { capabilities = ["read"] }

The transit key MUST exist before first use. Provision via:

    vault write -f transit/keys/digitorn-master type=aes256-gcm96
"""

from __future__ import annotations

import base64
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


class VaultTransitProvider:
    """Implements `MasterKeyProvider` via HashiCorp Vault Transit engine."""

    def __init__(
        self,
        vault_addr: str | None = None,
        token: str | None = None,
        role_id: str | None = None,
        secret_id: str | None = None,
        transit_key: str | None = None,
        mount_point: str | None = None,
        namespace: str | None = None,
    ) -> None:
        self._addr = vault_addr or os.environ.get("VAULT_ADDR", "")
        self._token = token or os.environ.get("VAULT_TOKEN")
        self._role_id = role_id or os.environ.get("VAULT_ROLE_ID")
        self._secret_id = secret_id or os.environ.get("VAULT_SECRET_ID")
        self._transit_key = transit_key or os.environ.get(
            "VAULT_TRANSIT_KEY", "digitorn-master",
        )
        self._mount = mount_point or os.environ.get("VAULT_TRANSIT_MOUNT", "transit")
        self._namespace = namespace or os.environ.get("VAULT_NAMESPACE")
        if not self._addr:
            raise ValueError(
                "VAULT_ADDR env var or `vault_addr` arg required for "
                "VaultTransitProvider",
            )
        if not self._token and not (self._role_id and self._secret_id):
            raise ValueError(
                "Either VAULT_TOKEN or (VAULT_ROLE_ID + VAULT_SECRET_ID) "
                "required for VaultTransitProvider",
            )
        self._client: Any = None
        logger.info(
            "master_key_provider=vault addr=%s mount=%s key=%s namespace=%s",
            self._addr, self._mount, self._transit_key,
            self._namespace or "<root>",
        )

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import hvac
        except ImportError as exc:
            raise RuntimeError(
                "hvac is required for VaultTransitProvider. "
                "Install with `pip install hvac`.",
            ) from exc

        client = hvac.Client(url=self._addr, namespace=self._namespace)
        if self._token:
            client.token = self._token
        else:
            # AppRole login
            resp = client.auth.approle.login(
                role_id=self._role_id, secret_id=self._secret_id,
            )
            client.token = resp["auth"]["client_token"]
        if not client.is_authenticated():
            raise RuntimeError("Vault authentication failed")
        self._client = client
        return self._client

    @property
    def backend(self) -> KmsBackend:
        return KmsBackend.VAULT

    @property
    def wraps_data_key(self) -> bool:
        return True

    @property
    def key_id(self) -> str | None:
        return f"{self._addr}/{self._mount}/keys/{self._transit_key}"

    async def get_data_key(self) -> KeyMaterial:
        """Generate a fresh DEK locally then wrap via Vault Transit Encrypt."""
        import asyncio
        dek = _secrets.token_bytes(KEY_LEN)
        client = self._ensure_client()

        def _call() -> dict[str, Any]:
            return client.secrets.transit.encrypt_data(
                name=self._transit_key,
                plaintext=base64.b64encode(dek).decode("ascii"),
                mount_point=self._mount,
            )

        try:
            resp = await asyncio.to_thread(_call)
        except Exception as exc:
            logger.error("vault_encrypt_failed: %s", exc)
            raise

        wrapped_str = resp["data"]["ciphertext"]  # "vault:v1:..." format
        wrapped = wrapped_str.encode("utf-8")
        return KeyMaterial(
            data_key=dek,
            wrapped_dek=wrapped,
            backend=KmsBackend.VAULT,
            key_id=self.key_id,
        )

    async def unwrap_data_key(self, wrapped: bytes) -> KeyMaterial:
        """Call Vault Transit Decrypt to recover the plaintext DEK."""
        import asyncio
        client = self._ensure_client()

        wrapped_str = wrapped.decode("utf-8")

        def _call() -> dict[str, Any]:
            return client.secrets.transit.decrypt_data(
                name=self._transit_key,
                ciphertext=wrapped_str,
                mount_point=self._mount,
            )

        try:
            resp = await asyncio.to_thread(_call)
        except Exception as exc:
            logger.error("vault_decrypt_failed: %s", exc)
            raise UnwrapFailed(
                f"Vault Transit decrypt rejected the wrapped DEK: {exc}"
            ) from exc

        plaintext_b64 = resp["data"]["plaintext"]
        dek = base64.b64decode(plaintext_b64)
        return KeyMaterial(
            data_key=dek,
            wrapped_dek=wrapped,
            backend=KmsBackend.VAULT,
            key_id=self.key_id,
        )

    async def healthcheck(self) -> bool:
        import asyncio
        try:
            client = self._ensure_client()

            def _call() -> bool:
                resp = client.secrets.transit.read_key(
                    name=self._transit_key,
                    mount_point=self._mount,
                )
                return bool(resp.get("data"))

            return await asyncio.to_thread(_call)
        except Exception as exc:
            logger.warning("vault_healthcheck_failed: %s", exc)
            return False

    async def close(self) -> None:
        try:
            if self._client is not None and hasattr(self._client, "adapter"):
                # hvac uses requests; close the session.
                self._client.adapter.close()
        except Exception:
            pass
        self._client = None
