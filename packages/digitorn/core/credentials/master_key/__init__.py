"""Master key provider abstraction for credential encryption.

The master key is the AES-256 key used to encrypt every credential's
fields before it lands in the database. Where the key COMES FROM is
deployment-specific:

  - Dev / single-machine          → env var or local file
  - Cloud production              → AWS KMS / GCP KMS / Azure Key Vault
  - On-prem / regulated           → HashiCorp Vault Transit

This package exposes a uniform `MasterKeyProvider` protocol so the
rest of the daemon never cares which backend is configured. Boot-time
selection is driven by `DIGITORN_KMS` env var (or `kms.provider` in
the YAML config):

    DIGITORN_KMS=env             → EnvKeyProvider          (default)
    DIGITORN_KMS=file            → FileKeyProvider
    DIGITORN_KMS=aws_kms         → AwsKmsProvider
    DIGITORN_KMS=gcp_kms         → GcpKmsProvider
    DIGITORN_KMS=azure_kv        → AzureKeyVaultProvider
    DIGITORN_KMS=vault           → VaultTransitProvider

Each provider can OPTIONALLY support remote-side encryption (the data
key never leaves the KMS). When the provider sets `wraps_data_key=True`,
the cipher generates a per-record DEK (data encryption key), encrypts
the credential with it, then asks the KMS to wrap (encrypt) the DEK
before storing both ciphertext + wrapped DEK in the row. This is the
"envelope encryption" pattern - much safer than holding the master
key in process memory, and what AWS / GCP recommend.

The default provider (`EnvKeyProvider`) returns the raw 32-byte key
which is held in memory and used directly - acceptable for dev and
single-tenant deployments.
"""

from __future__ import annotations

from digitorn.core.credentials.master_key.provider import (
    MasterKeyProvider,
    KeyMaterial,
    KmsBackend,
    UnwrapFailed,
    NoSuchProvider,
)
from digitorn.core.credentials.master_key.env_provider import EnvKeyProvider
from digitorn.core.credentials.master_key.file_provider import FileKeyProvider
from digitorn.core.credentials.master_key.factory import build_provider_from_config

__all__ = [
    "MasterKeyProvider",
    "KeyMaterial",
    "KmsBackend",
    "UnwrapFailed",
    "NoSuchProvider",
    "EnvKeyProvider",
    "FileKeyProvider",
    "build_provider_from_config",
]
