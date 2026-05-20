"""AES-256-GCM encryption for credential fields."""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets as _secrets
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)


DEFAULT_KEY_PATH = Path.home() / ".digitorn" / "master.key"
ENV_VAR_NAME = "DIGITORN_MASTER_KEY"
KEY_LEN = 32  # AES-256
NONCE_LEN = 12  # Recommended size for GCM


class CredentialEncryptionError(Exception):
    """Raised when encryption or decryption fails."""


def load_or_create_master_key(path: Path | None = None) -> bytes:
    """Return the master key, auto-generating it on first boot if needed."""
    # 1. Env var override - lets Docker / k8s inject the key at boot
    env_key = os.environ.get(ENV_VAR_NAME)
    if env_key:
        try:
            raw = base64.urlsafe_b64decode(env_key.encode("ascii"))
        except Exception as exc:
            raise CredentialEncryptionError(
                f"{ENV_VAR_NAME} is not valid base64-urlsafe: {exc}"
            ) from exc
        if len(raw) != KEY_LEN:
            raise CredentialEncryptionError(
                f"{ENV_VAR_NAME} must decode to {KEY_LEN} bytes, got {len(raw)}"
            )
        logger.info("master key loaded from %s env var", ENV_VAR_NAME)
        return raw

    target = path or DEFAULT_KEY_PATH

    # 2. Existing file
    if target.is_file():
        try:
            b64 = target.read_text(encoding="ascii").strip()
            raw = base64.urlsafe_b64decode(b64.encode("ascii"))
        except Exception as exc:
            raise CredentialEncryptionError(
                f"failed to read master key from {target}: {exc}"
            ) from exc
        if len(raw) != KEY_LEN:
            raise CredentialEncryptionError(
                f"master key file has wrong length: expected {KEY_LEN}, got {len(raw)}"
            )
        return raw

    # 3. Generate and write
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = AESGCM.generate_key(bit_length=KEY_LEN * 8)
    b64 = base64.urlsafe_b64encode(raw).decode("ascii")
    target.write_text(b64, encoding="ascii")
    try:
        # 0600 on POSIX - on Windows this is a no-op but harmless.
        os.chmod(target, 0o600)
    except (OSError, NotImplementedError):
        pass
    logger.warning(
        "generated new master key at %s - back it up, losing it means "
        "every stored credential becomes unreadable",
        target,
    )
    return raw


class Cipher:
    """Thin wrapper around AESGCM for encrypt/decrypt of a JSON dict."""

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_LEN:
            raise CredentialEncryptionError(
                f"master key must be {KEY_LEN} bytes, got {len(key)}"
            )
        self._aes = AESGCM(key)

    def encrypt(self, fields: dict[str, Any]) -> tuple[bytes, bytes]:
        """Encrypt a JSON-serialisable dict. Returns (ciphertext, nonce)."""
        try:
            payload = json.dumps(fields, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CredentialEncryptionError(
                f"cannot JSON-encode credential fields: {exc}"
            ) from exc
        nonce = _secrets.token_bytes(NONCE_LEN)
        ciphertext = self._aes.encrypt(nonce, payload, associated_data=None)
        return ciphertext, nonce

    def decrypt(self, ciphertext: bytes, nonce: bytes) -> dict[str, Any]:
        """Decrypt a (ciphertext, nonce) pair into the original dict."""
        try:
            plaintext = self._aes.decrypt(nonce, ciphertext, associated_data=None)
        except Exception as exc:
            raise CredentialEncryptionError(
                f"credential decryption failed - wrong master key or "
                f"tampered ciphertext: {exc}"
            ) from exc
        try:
            return json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialEncryptionError(
                f"decrypted payload is not valid JSON: {exc}"
            ) from exc


# Helpers for display - mask a secret without decrypting it


def mask_secret(value: str, keep: int = 4) -> str:
    """Return a masked preview of a secret for display purposes."""
    if not isinstance(value, str) or len(value) <= keep + 3:
        return "****"
    prefix = value[:3]
    suffix = value[-keep:]
    return f"{prefix}...{suffix}"


# Key rotation - manual, used by a CLI command, not the runtime


def rotate_master_key(
    old_key: bytes,
    new_key: bytes | None = None,
    path: Path | None = None,
) -> bytes:
    """Generate a new master key and write it to disk."""
    target = path or DEFAULT_KEY_PATH
    new = new_key or AESGCM.generate_key(bit_length=KEY_LEN * 8)
    b64 = base64.urlsafe_b64encode(new).decode("ascii")

    # Write to a temporary sibling file then rename, so a crash
    # mid-write doesn't leave a half-broken key file.
    tmp = target.with_suffix(target.suffix + ".new")
    tmp.write_text(b64, encoding="ascii")
    try:
        os.chmod(tmp, 0o600)
    except (OSError, NotImplementedError):
        pass
    os.replace(tmp, target)

    return new
