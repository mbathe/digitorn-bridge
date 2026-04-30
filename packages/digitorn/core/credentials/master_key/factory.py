"""Factory: pick the right MasterKeyProvider from config.

Resolution order at boot:

1. Explicit `kms` block in YAML config (highest precedence).
2. `DIGITORN_KMS` env var.
3. `DIGITORN_MASTER_KEY` env var → EnvKeyProvider (back-compat).
4. Default: FileKeyProvider with `~/.digitorn/master.key`.

Each KMS backend has its own required env vars (documented in its
provider module). The factory raises `NoSuchProvider` for an unknown
backend identifier and lets the original `ValueError` propagate from
the provider's __init__ for missing config (so the operator sees
exactly what's missing).

Boot path also runs `await provider.healthcheck()` and refuses to
start if the KMS is unreachable, so misconfiguration surfaces
immediately instead of at the first credential request.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from digitorn.core.credentials.master_key.provider import (
    KmsBackend,
    MasterKeyProvider,
    NoSuchProvider,
)
from digitorn.core.credentials.master_key.env_provider import EnvKeyProvider
from digitorn.core.credentials.master_key.file_provider import FileKeyProvider

logger = logging.getLogger(__name__)


def build_provider_from_config(
    config: dict[str, Any] | None = None,
) -> MasterKeyProvider:
    """Build a MasterKeyProvider from configuration.

    `config` can be a dict shaped like:
        {"provider": "aws_kms", "key_id": "...", "region": "..."}
    or None (env-driven).

    Returns the constructed provider. The caller should await
    `provider.healthcheck()` immediately after to verify reachability.
    """
    cfg = dict(config or {})
    backend_str = (
        cfg.get("provider")
        or os.environ.get("DIGITORN_KMS")
        or _autodetect_legacy()
    )
    backend_str = (backend_str or KmsBackend.FILE.value).lower()

    if backend_str == KmsBackend.ENV.value:
        return EnvKeyProvider()
    if backend_str == KmsBackend.FILE.value:
        path = cfg.get("path")
        if path:
            from pathlib import Path
            return FileKeyProvider(path=Path(path))
        return FileKeyProvider()
    if backend_str == KmsBackend.AWS_KMS.value:
        from digitorn.core.credentials.master_key.aws_kms_provider import (
            AwsKmsProvider,
        )
        return AwsKmsProvider(
            key_id=cfg.get("key_id"),
            region=cfg.get("region"),
            profile=cfg.get("profile"),
        )
    if backend_str == KmsBackend.GCP_KMS.value:
        from digitorn.core.credentials.master_key.gcp_kms_provider import (
            GcpKmsProvider,
        )
        return GcpKmsProvider(key_name=cfg.get("key_name"))
    if backend_str == KmsBackend.AZURE_KV.value:
        from digitorn.core.credentials.master_key.azure_kv_provider import (
            AzureKeyVaultProvider,
        )
        return AzureKeyVaultProvider(
            vault_url=cfg.get("vault_url"),
            key_name=cfg.get("key_name"),
            key_version=cfg.get("key_version"),
            algorithm=cfg.get("algorithm"),
        )
    if backend_str == KmsBackend.VAULT.value:
        from digitorn.core.credentials.master_key.vault_provider import (
            VaultTransitProvider,
        )
        return VaultTransitProvider(
            vault_addr=cfg.get("vault_addr"),
            token=cfg.get("token"),
            role_id=cfg.get("role_id"),
            secret_id=cfg.get("secret_id"),
            transit_key=cfg.get("transit_key"),
            mount_point=cfg.get("mount_point"),
            namespace=cfg.get("namespace"),
        )

    raise NoSuchProvider(
        f"Unknown KMS provider: {backend_str!r}. "
        f"Supported: {[b.value for b in KmsBackend]}"
    )


def _autodetect_legacy() -> str | None:
    """Back-compat: if `DIGITORN_MASTER_KEY` is set but `DIGITORN_KMS`
    is not, default to env provider. Lets existing deployments keep
    working without YAML changes."""
    if os.environ.get("DIGITORN_MASTER_KEY"):
        return KmsBackend.ENV.value
    return None
