"""FileKeyProvider - master key from `~/.digitorn/master.key`."""

from __future__ import annotations

import base64
import logging
import os
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from digitorn.core.credentials.master_key.provider import (
    KeyMaterial,
    KmsBackend,
)

logger = logging.getLogger(__name__)


KEY_LEN = 32
DEFAULT_PATH = Path.home() / ".digitorn" / "master.key"


class FileKeyProvider:
    """Implements `MasterKeyProvider`. Loads/generates the key file at"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = (path or DEFAULT_PATH).expanduser().resolve()
        self._key = self._load_or_create(self._path)
        self._check_permissions(self._path)
        logger.info(
            "master_key_provider=file path=%s key_len=%d",
            self._path, KEY_LEN,
        )

    @staticmethod
    def _load_or_create(path: Path) -> bytes:
        """Read existing key or atomically create a new one."""
        if path.is_file():
            try:
                b64 = path.read_text(encoding="ascii").strip()
                raw = base64.urlsafe_b64decode(b64.encode("ascii"))
            except Exception as exc:
                raise ValueError(
                    f"failed to read master key from {path}: {exc}",
                ) from exc
            if len(raw) != KEY_LEN:
                raise ValueError(
                    f"master key file {path} has wrong length: "
                    f"expected {KEY_LEN}, got {len(raw)}",
                )
            return raw

        path.parent.mkdir(parents=True, exist_ok=True)
        raw = AESGCM.generate_key(bit_length=KEY_LEN * 8)
        b64 = base64.urlsafe_b64encode(raw).decode("ascii")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(b64, encoding="ascii")
        try:
            os.chmod(tmp, 0o600)
        except (OSError, NotImplementedError):
            pass
        os.replace(tmp, path)
        logger.warning(
            "generated new master key at %s - back it up safely; "
            "losing it makes every stored credential unreadable",
            path,
        )
        return raw

    @staticmethod
    def _check_permissions(path: Path) -> None:
        """Warn if the file is world-readable. POSIX only — Windows ACLs are not checked."""
        if sys.platform == "win32":
            return
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                logger.warning(
                    "master key file %s has loose permissions (%o); "
                    "expected 0600. Run `chmod 600 %s`.",
                    path, mode, path,
                )
        except (OSError, NotImplementedError):
            pass

    @property
    def backend(self) -> KmsBackend:
        return KmsBackend.FILE

    @property
    def wraps_data_key(self) -> bool:
        return False

    @property
    def key_id(self) -> str | None:
        return f"file:{self._path}"

    async def get_data_key(self) -> KeyMaterial:
        return self.get_data_key_sync()

    async def unwrap_data_key(self, wrapped: bytes) -> KeyMaterial:
        return self.unwrap_data_key_sync(wrapped)

    def get_data_key_sync(self) -> KeyMaterial:
        return KeyMaterial(
            data_key=self._key,
            wrapped_dek=None,
            backend=KmsBackend.FILE,
            key_id=self.key_id,
        )

    def unwrap_data_key_sync(self, wrapped: bytes) -> KeyMaterial:
        return KeyMaterial(
            data_key=self._key,
            wrapped_dek=None,
            backend=KmsBackend.FILE,
            key_id=self.key_id,
        )

    async def healthcheck(self) -> bool:
        try:
            AESGCM(self._key)
            return self._path.is_file()
        except Exception:
            return False

    async def close(self) -> None:
        try:
            self._key = b""
        except Exception as exc:
            logger.debug("file_provider best-effort block failed: %s", exc)
