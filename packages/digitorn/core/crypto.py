"""Shared encryption helpers for Digitorn.

Uses Fernet symmetric encryption (``cryptography`` package) for
encrypting secrets, OAuth tokens, and other sensitive data at rest.

The encryption key is stored at ``~/.digitorn/server.key`` and is
auto-generated on first use.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

_SERVER_KEY_PATH = Path.home() / ".digitorn" / "server.key"


def _get_or_create_key() -> bytes:
    """Load (or generate) the server-level Fernet encryption key.

    Uses O_CREAT | O_EXCL to atomically create the key file with 0o600
    permissions — prevents TOCTOU races where an attacker pre-creates
    the file with weaker permissions.
    """
    _SERVER_KEY_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(
            str(_SERVER_KEY_PATH),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        # Key already exists — validate permissions before reading.
        st = _SERVER_KEY_PATH.stat()
        if st.st_mode & 0o077:
            logger.warning(
                "Server key %s has loose permissions (%o), tightening to 0600",
                _SERVER_KEY_PATH, st.st_mode & 0o777,
            )
            _SERVER_KEY_PATH.chmod(0o600)
        return _SERVER_KEY_PATH.read_bytes().strip()

    key = Fernet.generate_key()
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    logger.info("Generated new server encryption key at %s", _SERVER_KEY_PATH)
    return key


_fernet_instance: Fernet | None = None


def _get_fernet() -> Fernet:
    """Return a cached Fernet instance."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    key = _get_or_create_key()
    _fernet_instance = Fernet(key)
    return _fernet_instance


def encrypt_value(plaintext: str) -> bytes:
    """Encrypt a string value. Returns bytes suitable for LargeBinary."""
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_value(ciphertext: bytes) -> str:
    """Decrypt bytes back to a string."""
    return _get_fernet().decrypt(ciphertext).decode("utf-8")
