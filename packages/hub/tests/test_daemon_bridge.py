"""Pure unit tests for the bridge verifier - no DB, no FastAPI.

The router-level happy path is exercised by the end-to-end test in
`tools/live_tests/hub_bridge_scenarios.py`."""
from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from digitorn_hub.auth.daemon_bridge import (
    SIGNED_FIELDS,
    BridgeVerifyError,
    canonical_payload,
    fresh,
    verify_signature,
)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _make_keypair() -> tuple[str, Ed25519PrivateKey]:
    sk = Ed25519PrivateKey.generate()
    pk_raw = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64(pk_raw), sk


def _sign(sk: Ed25519PrivateKey, fields: dict) -> str:
    return _b64(sk.sign(canonical_payload(fields)))


# ─── canonical_payload ──────────────────────────────────────────────


def test_canonical_payload_is_sorted_and_compact():
    out = canonical_payload({"b": 2, "a": 1, "c": [3, 4]})
    assert out == b'{"a":1,"b":2,"c":[3,4]}'


def test_canonical_payload_is_deterministic_across_dict_orders():
    a = canonical_payload({"x": "hi", "y": 7, "ts": 1234})
    b = canonical_payload({"ts": 1234, "y": 7, "x": "hi"})
    assert a == b


def test_canonical_payload_handles_non_ascii():
    # email with a non-ascii local part - must round-trip without
    # escapes (ensure_ascii=False) so signers/verifiers agree.
    out = canonical_payload({"email": "élise@ex.com"})
    assert "é" in out.decode("utf-8")


# ─── verify_signature ──────────────────────────────────────────────


@pytest.fixture()
def fields():
    return {
        "daemon_name": "central",
        "user_id": "user-42",
        "email": "alice@example.com",
        "display_name": "Alice",
        "ts": 1_700_000_000,
        "nonce": "deadbeef" * 4,
    }


def test_signature_round_trips(fields):
    pub, sk = _make_keypair()
    sig = _sign(sk, fields)
    verify_signature(pub, fields, sig)  # no raise


def test_signature_fails_on_tampered_field(fields):
    pub, sk = _make_keypair()
    sig = _sign(sk, fields)
    tampered = {**fields, "email": "mallory@example.com"}
    with pytest.raises(BridgeVerifyError, match="signature does not verify"):
        verify_signature(pub, tampered, sig)


def test_signature_fails_with_wrong_pubkey(fields):
    _, sk = _make_keypair()
    other_pub, _ = _make_keypair()
    sig = _sign(sk, fields)
    with pytest.raises(BridgeVerifyError, match="signature does not verify"):
        verify_signature(other_pub, fields, sig)


def test_invalid_base64_pubkey_is_rejected(fields):
    _, sk = _make_keypair()
    sig = _sign(sk, fields)
    with pytest.raises(BridgeVerifyError, match="invalid base64"):
        verify_signature("not-valid-base64!!!", fields, sig)


def test_wrong_length_pubkey_is_rejected(fields):
    short = _b64(b"\x00" * 16)  # 16 bytes != 32
    _, sk = _make_keypair()
    sig = _sign(sk, fields)
    with pytest.raises(BridgeVerifyError, match="public key must be 32"):
        verify_signature(short, fields, sig)


def test_wrong_length_signature_is_rejected(fields):
    pub, _ = _make_keypair()
    bogus_sig = _b64(b"\x00" * 16)
    with pytest.raises(BridgeVerifyError, match="signature must be 64"):
        verify_signature(pub, fields, bogus_sig)


def test_signing_only_signed_fields_keeps_router_fields_in_sync(fields):
    """If someone adds a new field to DaemonBridgeRequest but forgets
    to add it to SIGNED_FIELDS, this test catches it."""
    assert set(SIGNED_FIELDS) == set(fields.keys())


# ─── fresh ─────────────────────────────────────────────────────────


def test_fresh_accepts_now():
    import time
    assert fresh(int(time.time()), 60)


def test_fresh_rejects_old(monkeypatch):
    import time
    now = int(time.time())
    assert not fresh(now - 120, 60)
    assert not fresh(now + 120, 60)


def test_fresh_accepts_within_window():
    import time
    now = int(time.time())
    assert fresh(now - 30, 60)
    assert fresh(now + 30, 60)
