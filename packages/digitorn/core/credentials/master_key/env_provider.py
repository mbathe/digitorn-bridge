"""EnvKeyProvider - master key from `DIGITORN_MASTER_KEY` env var."""

from __future__ import annotations

import base64
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from digitorn.core.credentials.master_key.provider import (
    KeyMaterial,
    KmsBackend,
    MasterKeyProvider,
)

logger = logging.getLogger(__name__)


ENV_VAR_NAME = "DIGITORN_MASTER_KEY"
KEY_LEN = 32


class EnvKeyProvider:
    """Implements `MasterKeyProvider`. Reads `DIGITORN_MASTER_KEY` at"""

    def __init__(self, env_var: str = ENV_VAR_NAME) -> None:
        raw_b64 = os.environ.get(env_var, "").strip()
        if not raw_b64:
            raise ValueError(
                f"{env_var} env var not set - cannot use EnvKeyProvider",
            )
        try:
            raw = base64.urlsafe_b64decode(raw_b64.encode("ascii"))
        except Exception as exc:
            raise ValueError(
                f"{env_var} is not valid base64-urlsafe: {exc}",
            ) from exc
        if len(raw) != KEY_LEN:
            raise ValueError(
                f"{env_var} must decode to {KEY_LEN} bytes, got {len(raw)}",
            )
        self._key = raw
        self._env_var = env_var
        logger.info(
            "master_key_provider=env env_var=%s key_len=%d",
            env_var, KEY_LEN,
        )

    @property
    def backend(self) -> KmsBackend:
        return KmsBackend.ENV

    @property
    def wraps_data_key(self) -> bool:
        return False  # direct mode

    @property
    def key_id(self) -> str | None:
        return f"env:{self._env_var}"

    async def get_data_key(self) -> KeyMaterial:
        return self.get_data_key_sync()

    async def unwrap_data_key(self, wrapped: bytes) -> KeyMaterial:
        return self.unwrap_data_key_sync(wrapped)

    def get_data_key_sync(self) -> KeyMaterial:
        return KeyMaterial(
            data_key=self._key,
            wrapped_dek=None,
            backend=KmsBackend.ENV,
            key_id=self.key_id,
        )

    def unwrap_data_key_sync(self, wrapped: bytes) -> KeyMaterial:
        return KeyMaterial(
            data_key=self._key,
            wrapped_dek=None,
            backend=KmsBackend.ENV,
            key_id=self.key_id,
        )

    async def healthcheck(self) -> bool:
        # In-memory key - trivially healthy if init succeeded.
        # Probe AESGCM construction to catch any pathological data.
        try:
            AESGCM(self._key)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        try:
            self._key = b""
        except Exception as exc:
            logger.debug("env_provider best-effort block failed: %s", exc)
