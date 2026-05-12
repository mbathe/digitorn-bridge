"""Multi-issuer JWT validation.

Bug #3: prior to this fix the gateway hardcoded a single ``iss``
value, so a JWT with ``iss=digitorn`` would be rejected when the
gateway's ``auth_issuer`` was set to ``https://api.digitorn.ai`` -
even though both names point at the same auth service. The fix
adds ``auth_accept_issuers: list[str]`` so operators can declare
additional accepted issuers without losing the primary check.
"""
from __future__ import annotations

import pytest


def test_settings_exposes_auth_accept_issuers_list(monkeypatch, tmp_path):
    """The Settings model must expose ``auth_accept_issuers`` as a
    plain ``list[str]`` so operators can override it via
    ``DIGITORN_GATEWAY_AUTH_ACCEPT_ISSUERS``.

    Hermetic: redirect ``Path.home()`` to a tmp dir so the test never
    reads the operator's real ``~/.digitorn/gateway.env`` (which may
    set the field for the running daemon and would skew the default).
    """
    monkeypatch.setattr(
        "digitorn_gateway.config.Path.home", lambda: tmp_path,
    )
    monkeypatch.delenv("DIGITORN_GATEWAY_AUTH_ACCEPT_ISSUERS", raising=False)
    # Re-import so the env_file path in SettingsConfigDict is re-evaluated.
    import importlib
    import digitorn_gateway.config as cfg
    importlib.reload(cfg)
    s = cfg.Settings()
    assert isinstance(s.auth_accept_issuers, list), (
        f"expected list, got {type(s.auth_accept_issuers).__name__}"
    )
    # Default should be empty (only the primary auth_issuer is
    # accepted out of the box).
    assert s.auth_accept_issuers == [], (
        f"default should be empty list, got {s.auth_accept_issuers!r}"
    )


def test_settings_auth_accept_issuers_via_env(monkeypatch):
    """Env override populates the list."""
    monkeypatch.setenv(
        "DIGITORN_GATEWAY_AUTH_ACCEPT_ISSUERS",
        '["digitorn", "https://auth.example.com"]',
    )
    from digitorn_gateway.config import Settings
    s = Settings()
    assert "digitorn" in s.auth_accept_issuers
    assert "https://auth.example.com" in s.auth_accept_issuers


@pytest.mark.asyncio
async def test_verify_token_accepts_alt_issuer():
    """When the token's ``iss`` matches an entry in
    ``accept_issuers`` (but not ``expected_issuer``), the verify
    function MUST NOT raise."""
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    # Build a self-signed RSA key so we can mint test tokens.
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    # Mint a token with iss="digitorn" (legacy label)
    import time
    token = pyjwt.encode(
        {
            "sub": "test-user",
            "iss": "digitorn",
            "iat": int(time.time()),
            "type": "access",
        },
        priv_pem,
        algorithm="RS256",
        headers={"kid": "test-kid"},
    )

    # Build a fake JWKS cache exposing our public key under "test-kid".
    from digitorn_gateway import auth as auth_mod
    from cryptography.hazmat.primitives.asymmetric.rsa import (
        RSAPublicKey,
    )

    class _FakeCachedKey:
        public_key: RSAPublicKey = pub

    class _FakeCache:
        empty = False
        def find(self, kid):
            return _FakeCachedKey() if kid == "test-kid" else None
        async def fetch(self):
            return None

    auth_mod._jwks = _FakeCache()  # type: ignore[attr-defined]

    # Verify with primary=https://api.digitorn.ai but accept_issuers
    # including "digitorn" - this should succeed.
    claims = await auth_mod._verify_token(
        token,
        expected_issuer="https://api.digitorn.ai",
        accept_issuers=("digitorn",),
    )
    assert claims["sub"] == "test-user"
    assert claims["iss"] == "digitorn"


@pytest.mark.asyncio
async def test_verify_token_rejects_unknown_issuer():
    """A token whose ``iss`` matches NEITHER ``expected_issuer`` NOR
    ``accept_issuers`` MUST be rejected with a clear 401."""
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    import time
    token = pyjwt.encode(
        {
            "sub": "test-user",
            "iss": "https://attacker.example.com",
            "iat": int(time.time()),
            "type": "access",
        },
        priv_pem,
        algorithm="RS256",
        headers={"kid": "test-kid-2"},
    )

    from digitorn_gateway import auth as auth_mod

    class _FakeCachedKey:
        public_key = pub

    class _FakeCache:
        empty = False
        def find(self, kid):
            return _FakeCachedKey() if kid == "test-kid-2" else None
        async def fetch(self):
            return None

    auth_mod._jwks = _FakeCache()  # type: ignore[attr-defined]

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as excinfo:
        await auth_mod._verify_token(
            token,
            expected_issuer="https://api.digitorn.ai",
            accept_issuers=("digitorn",),
        )
    assert excinfo.value.status_code == 401
    assert "issuer_mismatch" in str(excinfo.value.detail)
