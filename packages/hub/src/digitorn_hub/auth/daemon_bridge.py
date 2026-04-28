"""ed25519-signed bridge between trusted central daemons and the Hub.

Lives separately from the FastAPI router so the verification logic
can be unit-tested in isolation (no HTTP, no DB session).

Wire format
-----------
The daemon serialises a `DaemonBridgeRequest` minus its `signature`
field as canonical JSON (sorted keys, no whitespace) and signs the
UTF-8 bytes with its ed25519 private key. The Hub:

1. Looks up the trusted daemon by `daemon_name`.
2. Re-builds the canonical bytes and verifies the signature.
3. Checks `ts` is within the configured clock-skew window.
4. Inserts `nonce` into the per-daemon nonce table - duplicate
   key collision = replay rejected with 409.

Anything that doesn't pass becomes a 401 (signature) or 400 (shape).
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)


class BridgeVerifyError(Exception):
    """Signature did not verify or payload couldn't be canonicalised."""


def canonical_payload(fields: dict[str, Any]) -> bytes:
    """Sorted-key compact JSON of `fields`. The daemon and the Hub
    MUST produce identical bytes for the same logical payload."""
    return json.dumps(
        fields,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def verify_signature(
    public_key_b64: str,
    fields: dict[str, Any],
    signature_b64: str,
) -> None:
    """Raise [BridgeVerifyError] if the signature doesn't verify."""
    try:
        pub_raw = base64.b64decode(public_key_b64, validate=True)
        sig_raw = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise BridgeVerifyError(f"invalid base64: {exc}") from exc

    if len(pub_raw) != 32:
        raise BridgeVerifyError("public key must be 32 raw ed25519 bytes")
    if len(sig_raw) != 64:
        raise BridgeVerifyError("signature must be 64 raw ed25519 bytes")

    msg = canonical_payload(fields)
    try:
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig_raw, msg)
    except InvalidSignature as exc:
        raise BridgeVerifyError("signature does not verify") from exc


def fresh(ts: int, max_skew_seconds: int) -> bool:
    """Return True iff `ts` is within `±max_skew_seconds` of now."""
    now = int(datetime.now(timezone.utc).timestamp())
    return abs(now - ts) <= max_skew_seconds


# Keep canonical_payload's logical field set in one place so the daemon
# implementation can import the same constant. Order doesn't matter
# (sorted at serialisation time) - this is just the inventory.
SIGNED_FIELDS = ("daemon_name", "user_id", "email", "display_name", "ts", "nonce")
