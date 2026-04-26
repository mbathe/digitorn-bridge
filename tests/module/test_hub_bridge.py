"""Cross-process compat test: payloads signed by the daemon's
`hub_bridge` module verify against the Hub's `daemon_bridge` verifier.

Both modules MUST agree byte-for-byte on the canonical JSON
serialisation, otherwise every bridge call would fail in production.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from contextlib import contextmanager

from digitorn.core.api import hub_bridge as daemon_side
from digitorn.core.config import Settings, get_settings, override_settings
from digitorn_hub.auth.daemon_bridge import (
    SIGNED_FIELDS,
    BridgeVerifyError,
    canonical_payload,
    verify_signature,
)


@contextmanager
def _bridge_settings(daemon_name: str = "central"):
    """Swap in a Settings with the bridge enabled, restore afterwards."""
    original = get_settings()
    overridden = original.model_copy(deep=True)
    overridden.hub.daemon_bridge.enabled = True
    overridden.hub.daemon_bridge.daemon_name = daemon_name
    override_settings(overridden)
    try:
        yield
    finally:
        override_settings(original)


@pytest.fixture()
def keypair_in(tmp_path: Path) -> Path:
    """Create a keypair under tmp_path and point the daemon at it."""
    sk_path = tmp_path / "central.sk"
    sk = Ed25519PrivateKey.generate()
    sk_raw = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    sk_path.write_text(base64.b64encode(sk_raw).decode("ascii") + "\n")

    pk_raw = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    (tmp_path / "central.pub").write_text(
        base64.b64encode(pk_raw).decode("ascii") + "\n"
    )
    return sk_path


def test_canonical_payload_matches_across_modules(monkeypatch, keypair_in):
    monkeypatch.setattr(daemon_side, "_resolve_key_path", lambda: keypair_in)

    # Pick the same logical fields in both modules.
    fields = {
        "daemon_name": "central",
        "user_id": "u-1",
        "email": "alice@example.com",
        "display_name": "Alice",
        "ts": 1_700_000_000,
        "nonce": "deadbeef" * 4,
    }
    daemon_canonical = daemon_side._canonical(fields)
    hub_canonical = canonical_payload(fields)
    assert daemon_canonical == hub_canonical


def test_signed_request_verifies_with_hub_verifier(
    monkeypatch, keypair_in, tmp_path
):
    monkeypatch.setattr(daemon_side, "_resolve_key_path", lambda: keypair_in)
    # Disable bridge by default — we're testing the signing primitives,
    # not the runtime gating.
    with _bridge_settings():
        signed = daemon_side._sign_request(
            user_id="u-1",
            email="alice@example.com",
            display_name="Alice",
        )

    # Pull just the signed fields back out + the signature.
    fields = {k: signed[k] for k in SIGNED_FIELDS}
    sig = signed["signature"]
    pub = (tmp_path / "central.pub").read_text().strip()

    # The Hub-side verifier must accept it.
    verify_signature(pub, fields, sig)


def test_tampering_fields_breaks_signature(monkeypatch, keypair_in, tmp_path):
    monkeypatch.setattr(daemon_side, "_resolve_key_path", lambda: keypair_in)
    with _bridge_settings():
        signed = daemon_side._sign_request(
            user_id="u-1",
            email="alice@example.com",
            display_name="Alice",
        )
    fields = {k: signed[k] for k in SIGNED_FIELDS}
    fields["email"] = "mallory@example.com"
    pub = (tmp_path / "central.pub").read_text().strip()
    with pytest.raises(BridgeVerifyError):
        verify_signature(pub, fields, signed["signature"])


def test_signed_fields_inventory_is_exact():
    """If we add a field to one side without the other, this dies."""
    assert daemon_side.SIGNED_FIELDS == SIGNED_FIELDS


def test_timestamp_is_close_to_now(monkeypatch, keypair_in):
    monkeypatch.setattr(daemon_side, "_resolve_key_path", lambda: keypair_in)
    with _bridge_settings():
        signed = daemon_side._sign_request(
            user_id="u-1",
            email="alice@example.com",
            display_name=None,
        )
    assert abs(int(time.time()) - signed["ts"]) < 5


def test_nonce_is_unique_each_call(monkeypatch, keypair_in):
    monkeypatch.setattr(daemon_side, "_resolve_key_path", lambda: keypair_in)
    with _bridge_settings():
        a = daemon_side._sign_request(user_id="u-1", email="a@b.c", display_name=None)
        b = daemon_side._sign_request(user_id="u-1", email="a@b.c", display_name=None)
    assert a["nonce"] != b["nonce"]
