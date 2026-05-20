"""VersionedCipher - AES-256-GCM with a versioned envelope header."""

from __future__ import annotations

import json
import logging
import secrets as _secrets
import struct
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from digitorn.core.credentials.master_key.provider import (
    KmsBackend,
    MasterKeyProvider,
    UnwrapFailed,
)

logger = logging.getLogger(__name__)


CIPHER_VERSION_V1 = 0x01
NONCE_LEN = 12
KEY_LEN = 32
HEADER_FMT = ">BBBH"  # version, flags, backend, wrapped_len
HEADER_LEN = struct.calcsize(HEADER_FMT)
FLAG_ENVELOPE = 0x01


# Map enum to/from a byte for the header. Adding a backend = bumping
# this list (never reorder).
_BACKEND_ORDER = [
    KmsBackend.ENV,
    KmsBackend.FILE,
    KmsBackend.AWS_KMS,
    KmsBackend.GCP_KMS,
    KmsBackend.AZURE_KV,
    KmsBackend.VAULT,
]


def _backend_to_byte(b: KmsBackend) -> int:
    try:
        return _BACKEND_ORDER.index(b)
    except ValueError:
        return 0xFF  # unknown - shouldn't happen


def _byte_to_backend(b: int) -> KmsBackend:
    if 0 <= b < len(_BACKEND_ORDER):
        return _BACKEND_ORDER[b]
    raise ValueError(f"unknown backend byte: {b:#x}")


class CipherError(Exception):
    """Raised on any encrypt/decrypt failure that the caller should"""


class VersionedCipher:
    """Async cipher that pulls keys from a MasterKeyProvider."""

    def __init__(self, provider: MasterKeyProvider) -> None:
        self._provider = provider

    @property
    def provider(self) -> MasterKeyProvider:
        return self._provider

    async def encrypt(self, fields: dict[str, Any]) -> tuple[bytes, bytes]:
        """Encrypt a JSON-serialisable dict."""
        try:
            plaintext = json.dumps(fields, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CipherError(
                f"cannot JSON-encode credential fields: {exc}",
            ) from exc

        material = await self._provider.get_data_key()
        if len(material.data_key) != KEY_LEN:
            raise CipherError(
                f"data key wrong length: expected {KEY_LEN}, "
                f"got {len(material.data_key)}",
            )

        nonce = _secrets.token_bytes(NONCE_LEN)
        try:
            aes = AESGCM(material.data_key)
            ciphertext = aes.encrypt(nonce, plaintext, associated_data=None)
        except Exception as exc:
            raise CipherError(f"AES-GCM encrypt failed: {exc}") from exc
        finally:
            self._zeroize(material.data_key)

        is_envelope = self._provider.wraps_data_key
        flags = FLAG_ENVELOPE if is_envelope else 0
        wrapped = material.wrapped_dek or b""
        if is_envelope and not wrapped:
            raise CipherError(
                "envelope-mode provider returned no wrapped DEK",
            )
        if len(wrapped) > 0xFFFF:
            raise CipherError(
                f"wrapped DEK too large for header: {len(wrapped)} bytes",
            )

        header = struct.pack(
            HEADER_FMT,
            CIPHER_VERSION_V1,
            flags,
            _backend_to_byte(material.backend),
            len(wrapped),
        )
        payload = header + wrapped + ciphertext
        return payload, nonce

    async def decrypt(self, payload: bytes, nonce: bytes) -> dict[str, Any]:
        """Decrypt a versioned payload back to the original dict."""
        # Detect legacy payloads: they are raw AES-GCM ciphertext, no
        # header. Heuristic: if byte0 is not 0x01, treat as legacy.
        if not payload:
            raise CipherError("empty payload")

        if payload[0] != CIPHER_VERSION_V1:
            return await self._decrypt_legacy(payload, nonce)

        if len(payload) < HEADER_LEN:
            raise CipherError("payload shorter than header")

        version, flags, backend_byte, wrapped_len = struct.unpack(
            HEADER_FMT, payload[:HEADER_LEN],
        )
        body_start = HEADER_LEN + wrapped_len
        if len(payload) < body_start:
            raise CipherError("payload truncated before wrapped DEK")

        wrapped = payload[HEADER_LEN:body_start] if wrapped_len else None
        ciphertext = payload[body_start:]

        is_envelope = bool(flags & FLAG_ENVELOPE)
        if is_envelope:
            if wrapped is None:
                raise CipherError(
                    "envelope flag set but no wrapped DEK in payload",
                )
            try:
                material = await self._provider.unwrap_data_key(wrapped)
            except UnwrapFailed as exc:
                raise CipherError(
                    f"KMS refused to unwrap DEK: {exc}",
                ) from exc
        else:
            material = await self._provider.get_data_key()

        try:
            aes = AESGCM(material.data_key)
            plaintext = aes.decrypt(nonce, ciphertext, associated_data=None)
        except Exception as exc:
            raise CipherError(
                "AES-GCM decrypt failed (wrong key, tampered, or "
                "wrong nonce)",
            ) from exc
        finally:
            self._zeroize(material.data_key)

        try:
            return json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CipherError(
                f"decrypted payload is not valid JSON: {exc}",
            ) from exc

    async def _decrypt_legacy(
        self, ciphertext: bytes, nonce: bytes,
    ) -> dict[str, Any]:
        """Legacy path: payload is raw AES-GCM ciphertext encrypted"""
        if self._provider.wraps_data_key:
            raise CipherError(
                "legacy direct-mode payload found but current provider "
                "is envelope-mode (KMS). Run "
                "`digitorn credentials reencrypt` to migrate before "
                "switching to KMS.",
            )

        material = await self._provider.get_data_key()
        try:
            aes = AESGCM(material.data_key)
            plaintext = aes.decrypt(nonce, ciphertext, associated_data=None)
        except Exception as exc:
            raise CipherError(
                "legacy AES-GCM decrypt failed",
            ) from exc
        finally:
            self._zeroize(material.data_key)

        try:
            return json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CipherError(
                f"decrypted payload is not valid JSON: {exc}",
            ) from exc


    def encrypt_sync(self, fields: dict[str, Any]) -> tuple[bytes, bytes]:
        """Synchronous encrypt. Only valid for direct-mode providers"""
        if self._provider.wraps_data_key:
            raise CipherError(
                "encrypt_sync() is not available with envelope-mode "
                "providers (KMS). Use `await cipher.encrypt(...)`.",
            )
        try:
            plaintext = json.dumps(fields, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CipherError(
                f"cannot JSON-encode credential fields: {exc}",
            ) from exc
        try:
            material = self._provider.get_data_key_sync()
        except NotImplementedError as exc:
            raise CipherError(
                f"provider {self._provider.backend.value} does not "
                "support sync encrypt; use the async API",
            ) from exc
        if len(material.data_key) != KEY_LEN:
            raise CipherError(
                f"data key wrong length: expected {KEY_LEN}, "
                f"got {len(material.data_key)}",
            )
        nonce = _secrets.token_bytes(NONCE_LEN)
        try:
            aes = AESGCM(material.data_key)
            ciphertext = aes.encrypt(nonce, plaintext, associated_data=None)
        except Exception as exc:
            raise CipherError(f"AES-GCM encrypt failed: {exc}") from exc
        finally:
            self._zeroize(material.data_key)
        header = struct.pack(
            HEADER_FMT,
            CIPHER_VERSION_V1,
            0,  # flags=0 for direct mode
            _backend_to_byte(material.backend),
            0,  # wrapped_len=0 for direct mode
        )
        payload = header + ciphertext
        return payload, nonce

    def decrypt_sync(self, payload: bytes, nonce: bytes) -> dict[str, Any]:
        """Synchronous decrypt. Only valid for direct-mode providers."""
        if self._provider.wraps_data_key:
            raise CipherError(
                "decrypt_sync() is not available with envelope-mode "
                "providers (KMS). Use `await cipher.decrypt(...)`.",
            )
        if not payload:
            raise CipherError("empty payload")

        # Legacy detection: byte0 != 0x01 → pre-versioned format.
        if payload[0] != CIPHER_VERSION_V1:
            try:
                material = self._provider.get_data_key_sync()
            except NotImplementedError as exc:
                raise CipherError(
                    "provider does not support sync decrypt; use async API",
                ) from exc
            try:
                aes = AESGCM(material.data_key)
                plaintext = aes.decrypt(nonce, payload, associated_data=None)
            except Exception as exc:
                raise CipherError(
                    f"legacy AES-GCM decrypt failed: {exc}",
                ) from exc
            finally:
                self._zeroize(material.data_key)
            try:
                return json.loads(plaintext.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CipherError(
                    f"decrypted payload is not valid JSON: {exc}",
                ) from exc

        if len(payload) < HEADER_LEN:
            raise CipherError("payload shorter than header")
        version, flags, backend_byte, wrapped_len = struct.unpack(
            HEADER_FMT, payload[:HEADER_LEN],
        )
        body_start = HEADER_LEN + wrapped_len
        if len(payload) < body_start:
            raise CipherError("payload truncated before wrapped DEK")
        if flags & FLAG_ENVELOPE:
            raise CipherError(
                "envelope-flagged payload encountered in sync path; "
                "this row was written by a KMS provider - migrate to "
                "async cipher API or revert provider.",
            )
        ciphertext = payload[body_start:]
        try:
            material = self._provider.get_data_key_sync()
        except NotImplementedError as exc:
            raise CipherError(
                "provider does not support sync decrypt; use async API",
            ) from exc
        try:
            aes = AESGCM(material.data_key)
            plaintext = aes.decrypt(nonce, ciphertext, associated_data=None)
        except Exception as exc:
            raise CipherError(
                f"AES-GCM decrypt failed: {exc}",
            ) from exc
        finally:
            self._zeroize(material.data_key)
        try:
            return json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CipherError(
                f"decrypted payload is not valid JSON: {exc}",
            ) from exc

    @staticmethod
    def _zeroize(key: bytes) -> None:
        """Best-effort attempt to drop the data key from memory."""
        # Intentionally a no-op for now. Hook for future hardening.
        del key


