"""MasterKeyProvider protocol + shared types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class KmsBackend(str, Enum):
    """Identifier of the backend used. Persisted alongside ciphertext"""

    ENV = "env"
    FILE = "file"
    AWS_KMS = "aws_kms"
    GCP_KMS = "gcp_kms"
    AZURE_KV = "azure_kv"
    VAULT = "vault"


@dataclass(frozen=True)
class KeyMaterial:
    """Data flowing between cipher and provider."""

    data_key: bytes
    wrapped_dek: bytes | None = None
    backend: KmsBackend = KmsBackend.ENV
    key_id: str | None = None  # KMS key ARN/URN for audit trail


class UnwrapFailed(Exception):
    """KMS refused to unwrap a wrapped DEK. Possible causes:"""


class NoSuchProvider(Exception):
    """Raised by `build_provider_from_config` when the requested"""


@runtime_checkable
class MasterKeyProvider(Protocol):
    """Uniform interface for every master-key source."""

    @property
    def backend(self) -> KmsBackend:
        """Which backend identifier to persist alongside ciphertext."""
        ...

    @property
    def wraps_data_key(self) -> bool:
        """When True, the cipher uses envelope mode: generate per-record"""
        ...

    @property
    def key_id(self) -> str | None:
        """Identifier of the master key, for audit. KMS providers"""
        ...

    async def get_data_key(self) -> KeyMaterial:
        """Return the data key to use for encryption."""
        ...

    async def unwrap_data_key(self, wrapped: bytes) -> KeyMaterial:
        """Recover the plain DEK from its wrapped form."""
        ...

    async def healthcheck(self) -> bool:
        """Lightweight probe: is the backend reachable + the key"""
        ...

    async def close(self) -> None:
        """Release any resources held by the provider (HTTP clients,"""
        ...

    def get_data_key_sync(self) -> KeyMaterial:
        """Synchronous variant for direct-mode providers (env/file)."""
        raise NotImplementedError(
            "get_data_key_sync is only available on direct-mode providers"
        )

    def unwrap_data_key_sync(self, wrapped: bytes) -> KeyMaterial:
        """Synchronous variant of `unwrap_data_key`. Same contract as"""
        raise NotImplementedError(
            "unwrap_data_key_sync is only available on direct-mode providers"
        )
