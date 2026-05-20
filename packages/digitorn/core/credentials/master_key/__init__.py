"""Master key provider abstraction for credential encryption."""

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
