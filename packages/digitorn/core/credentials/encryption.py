"""AES-256-GCM encryption for credential fields.

Every credential's ``fields`` dict is JSON-encoded then encrypted with
AES-256-GCM before it lands in the database. The master key lives in
``~/.digitorn/master.key`` (mode 0600, never committed). It is
auto-generated on the first boot and never rotated automatically —
rotation is a manual operation (see ``rotate_master_key`` in this
module, used by the CLI command ``digitorn credentials rotate-key``).

Design choices:

- **AES-256-GCM** because it is authenticated (detects tampering),
  fast, and standard. ChaCha20-Poly1305 would also work.
- **Per-record nonce**: each encrypt call generates a fresh 12-byte
  nonce and stores it alongside the ciphertext. Never reuse a nonce
  with the same key.
- **JSON serialisation of the field dict**: we encrypt the whole
  ``{field_name: value}`` blob as one payload, not per field, so a
  single AES-GCM call covers an entire credential and the key
  handlers work with plain dicts.
- **Master key override via env var**: ``DIGITORN_MASTER_KEY`` can be
  set at daemon startup to use a base64-encoded key from the
  environment (useful for Docker / Kubernetes where secrets come from
  the orchestrator). The env var takes precedence over the file.
- **Base64-urlsafe** for the on-disk representation so humans can
  inspect the file without hex-decoding.
"""

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
    """Return the master key, auto-generating it on first boot if needed.

    Priority order:

    1. ``DIGITORN_MASTER_KEY`` env var (base64-urlsafe, 32 bytes after decoding)
    2. ``<path>`` file (default: ``~/.digitorn/master.key``)
    3. Generate a new key and write it to ``<path>`` (mode 0600)

    Returns the raw 32-byte key.
    """
    # 1. Env var override — lets Docker / k8s inject the key at boot
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
        # 0600 on POSIX — on Windows this is a no-op but harmless.
        os.chmod(target, 0o600)
    except (OSError, NotImplementedError):
        pass
    logger.warning(
        "generated new master key at %s — back it up, losing it means "
        "every stored credential becomes unreadable",
        target,
    )
    return raw


class Cipher:
    """Thin wrapper around AESGCM for encrypt/decrypt of a JSON dict.

    Usage::

        cipher = Cipher(master_key_bytes)
        ciphertext, nonce = cipher.encrypt({"api_key": "sk-..."})
        plaintext = cipher.decrypt(ciphertext, nonce)
    """

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
                f"credential decryption failed — wrong master key or "
                f"tampered ciphertext: {exc}"
            ) from exc
        try:
            return json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialEncryptionError(
                f"decrypted payload is not valid JSON: {exc}"
            ) from exc


# ────────────────────────────────────────────────────────────────────
# Helpers for display — mask a secret without decrypting it
# ────────────────────────────────────────────────────────────────────


def mask_secret(value: str, keep: int = 4) -> str:
    """Return a masked preview of a secret for display purposes.

    Shows the last ``keep`` characters preceded by a sentinel. Used by
    the HTTP routes to tell the Flutter client "this field is set"
    without leaking the real value.

    Examples::

        mask_secret("sk-ant-abcdefghij")   → "sk-...hij*"
        mask_secret("xyz", keep=4)          → "****"  (too short)
    """
    if not isinstance(value, str) or len(value) <= keep + 3:
        return "****"
    prefix = value[:3]
    suffix = value[-keep:]
    return f"{prefix}...{suffix}"


# ────────────────────────────────────────────────────────────────────
# Key rotation — manual, used by a CLI command, not the runtime
# ────────────────────────────────────────────────────────────────────


def rotate_master_key(
    old_key: bytes,
    new_key: bytes | None = None,
    path: Path | None = None,
) -> bytes:
    """Generate a new master key and write it to disk.

    The caller is responsible for **re-encrypting every stored
    credential** with the new key before the old one is lost — this
    function only writes the new key file. Returns the new key.

    Typically wired to a ``digitorn credentials rotate-key`` CLI
    command that:

    1. Calls this function to generate the new key
    2. Opens the credential store with the old key
    3. Decrypts each record + re-encrypts with the new key
    4. Replaces the file with the new key at the end
    """
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
