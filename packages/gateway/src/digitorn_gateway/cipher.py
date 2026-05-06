"""Envelope encryption for gateway credentials.

The dashboard lets operators paste raw provider keys (Anthropic, OpenAI,
DeepSeek, ...) which the gateway then uses to dispatch LLM calls. Those
keys MUST NOT touch disk in plaintext: a casual ``pg_dump`` would leak
the entire fleet's billing surface.

Design:

  * One process-wide MASTER KEY (32 bytes) loaded from env at boot.
    In prod it should come from a KMS (AWS / GCP / Azure / Vault); we
    accept ``DIGITORN_GATEWAY_MASTER_KEY`` (32-byte b64url) for the
    bootstrap and dev cases.
  * Per-row Data Encryption Key (DEK): 32 random bytes generated at
    write time. The DEK encrypts the payload with AES-256-GCM and is
    itself wrapped with the master key (also AES-256-GCM). The wrapped
    DEK is stored alongside the ciphertext.
  * Layout of ``encrypted_value`` in DB (BYTEA):

        version(1B) | nonce_master(12B) | wrapped_dek(48B)
                    | nonce_data(12B)  | ciphertext+tag(N+16B)

    Total = 1 + 12 + 48 + 12 + 16 + N = 89 + N bytes.

Key rotation: bumping the master key only requires re-encrypting the
wrapped_dek of every row (the DEKs themselves are unchanged). The
``rewrap_with_new_master`` helper does this in a single pass.

Threat model: this protects the credential payload at rest. It does
NOT protect against a process that has the master key in memory; that
process can decrypt anything it wants. Use container isolation +
network segmentation to keep the gateway's process boundary tight.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from typing import Optional

logger = logging.getLogger(__name__)


_VERSION_BYTE = 1
_NONCE_BYTES = 12
_WRAPPED_DEK_BYTES = 32 + 16  # DEK + AES-GCM tag


class GatewayCipherError(RuntimeError):
    """Raised on any cipher-level failure (bad master, bad payload, etc.)."""


# ── Master key resolution ─────────────────────────────────────────────


_master: Optional[bytes] = None


def _load_master() -> bytes:
    """Resolve the master key from ``DIGITORN_GATEWAY_MASTER_KEY``.

    Accepted formats: 32-byte raw, hex-32, or url-safe base64 (with or
    without padding). Anything that decodes to exactly 32 bytes wins.
    """
    raw = os.environ.get("DIGITORN_GATEWAY_MASTER_KEY", "").strip()
    if not raw:
        raise GatewayCipherError(
            "DIGITORN_GATEWAY_MASTER_KEY is not set. Generate one with: "
            "python -c \"import os, base64; "
            "print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
        )

    candidates: list[bytes] = []
    # Try raw bytes (will only match if exactly 32 chars long).
    candidates.append(raw.encode("utf-8"))
    # Try hex.
    try:
        candidates.append(bytes.fromhex(raw))
    except ValueError:
        pass
    # Try url-safe base64 (pad if needed).
    try:
        padded = raw + "=" * (-len(raw) % 4)
        candidates.append(base64.urlsafe_b64decode(padded))
    except Exception:
        pass
    # Try standard base64.
    try:
        padded = raw + "=" * (-len(raw) % 4)
        candidates.append(base64.b64decode(padded))
    except Exception:
        pass

    for c in candidates:
        if len(c) == 32:
            return c

    raise GatewayCipherError(
        "DIGITORN_GATEWAY_MASTER_KEY must decode to exactly 32 bytes "
        "(hex / url-safe-b64 / standard-b64 all accepted)."
    )


def get_master() -> bytes:
    """Return the cached master key, loading it lazily on first call."""
    global _master
    if _master is None:
        _master = _load_master()
    return _master


def reload_master() -> None:
    """Force the master key to be re-loaded from env (tests)."""
    global _master
    _master = None


# ── Encrypt / Decrypt ─────────────────────────────────────────────────


def _aesgcm():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM


def encrypt(plaintext: str) -> bytes:
    """Wrap a UTF-8 plaintext into the envelope blob layout."""
    if not isinstance(plaintext, str):
        raise GatewayCipherError("encrypt() expects a str")
    AESGCM = _aesgcm()

    master = get_master()
    dek = secrets.token_bytes(32)
    nonce_master = secrets.token_bytes(_NONCE_BYTES)
    nonce_data = secrets.token_bytes(_NONCE_BYTES)

    wrapped_dek = AESGCM(master).encrypt(nonce_master, dek, b"")
    if len(wrapped_dek) != _WRAPPED_DEK_BYTES:
        # Defensive: AES-GCM tag is fixed 16 bytes; this should never trip.
        raise GatewayCipherError(
            f"unexpected wrapped DEK size: {len(wrapped_dek)} bytes"
        )

    ciphertext = AESGCM(dek).encrypt(
        nonce_data, plaintext.encode("utf-8"), b"",
    )
    return (
        bytes([_VERSION_BYTE])
        + nonce_master
        + wrapped_dek
        + nonce_data
        + ciphertext
    )


def decrypt(blob: bytes) -> str:
    """Unwrap an envelope blob produced by :func:`encrypt`."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise GatewayCipherError(
            f"decrypt() expects bytes-like, got {type(blob).__name__}",
        )
    blob = bytes(blob)
    min_size = 1 + _NONCE_BYTES + _WRAPPED_DEK_BYTES + _NONCE_BYTES + 16
    if len(blob) < min_size:
        raise GatewayCipherError(
            f"ciphertext too short: {len(blob)} < {min_size}",
        )
    version = blob[0]
    if version != _VERSION_BYTE:
        raise GatewayCipherError(f"unsupported cipher version: {version}")

    AESGCM = _aesgcm()
    p = 1
    nonce_master = blob[p : p + _NONCE_BYTES]
    p += _NONCE_BYTES
    wrapped_dek = blob[p : p + _WRAPPED_DEK_BYTES]
    p += _WRAPPED_DEK_BYTES
    nonce_data = blob[p : p + _NONCE_BYTES]
    p += _NONCE_BYTES
    ciphertext = blob[p:]

    master = get_master()
    try:
        dek = AESGCM(master).decrypt(nonce_master, wrapped_dek, b"")
    except Exception as exc:
        raise GatewayCipherError(
            "wrapped DEK does not match master key (rotation? wrong env?)",
        ) from exc

    try:
        plaintext = AESGCM(dek).decrypt(nonce_data, ciphertext, b"")
    except Exception as exc:
        raise GatewayCipherError("ciphertext authentication failed") from exc

    return plaintext.decode("utf-8")


def mask(plaintext: str) -> str:
    """Return a masked preview of a credential for display.
    Keeps first 6 + last 4 chars; the middle becomes a fixed bullet
    string. Used when the dashboard shows a "verify" / "rotate" hint."""
    if len(plaintext) <= 12:
        return "***"
    return f"{plaintext[:6]}***{plaintext[-4:]}"


# ── Dict helpers (multi-field auth) ───────────────────────────────────


def encrypt_dict(secret: dict[str, str]) -> bytes:
    """Wrap a dict of fields (e.g. {'access_token': '...', 'refresh_token': '...'})
    using the same envelope format as :func:`encrypt`. We serialise via JSON
    so the on-disk format is human-debuggable once decrypted."""
    import json as _json
    return encrypt(_json.dumps(secret, separators=(",", ":")))


def decrypt_dict(blob: bytes) -> dict[str, str]:
    """Reverse of :func:`encrypt_dict`. Falls back to ``{"value": <str>}``
    for legacy rows that were written by the single-string code path,
    so existing credentials keep working after the auth_type upgrade."""
    import json as _json
    plaintext = decrypt(blob)
    try:
        parsed = _json.loads(plaintext)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except Exception:
        pass
    # Legacy single-string row.
    return {"value": plaintext}


def mask_dict(secret: dict[str, str]) -> dict[str, str]:
    """Mask every value in ``secret``. Field names stay readable so the
    dashboard can label each row, but no plaintext leaves the gateway."""
    return {k: mask(v) for k, v in secret.items()}
