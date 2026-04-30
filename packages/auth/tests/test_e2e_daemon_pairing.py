"""End-to-end pairing: central auth service ↔ daemon-side LocalDeviceAuth.

Simulates what happens when a user runs ``digitorn install-local``:
the CLI fetches an access token, pairs the device, and persists the
returned device_token. Then we simulate the daemon booting OFFLINE
(no network) and validating that token via ``LocalDeviceAuth.load()``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_full_pairing_then_offline_validate(app_and_client, tmp_path, monkeypatch):
    # Make the daemon-side modules importable (digitorn.core.auth.local_device)
    daemon_src = Path(__file__).resolve().parents[3] / "digitorn" / "packages"
    if daemon_src.exists():
        sys.path.insert(0, str(daemon_src))
    daemon_src2 = Path(__file__).resolve().parents[3] / "digitorn"
    sys.path.insert(0, str(daemon_src2))

    try:
        from digitorn.core.auth.local_device import (
            LocalDeviceAuth,
            NotPaired,
        )
    except ImportError:
        pytest.skip("daemon package not on sys.path - run from repo root")

    _, client = app_and_client

    # 1. Register a user
    reg = await client.post("/auth/register", json={
        "username": "daemon-owner",
        "password": "vveryltestpassword",
        "email": "owner@example.com",
    })
    assert reg.status_code == 200, reg.text
    access_token = reg.json()["access_token"]

    # 2. Pair this "daemon" via the access token
    pair = await client.post(
        "/auth/devices/pair",
        json={"label": "Test-Daemon"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert pair.status_code == 200, pair.text
    pair_data = pair.json()

    # 3. Fetch JWKS (what the CLI does after pairing) so the daemon
    #    can verify offline against the central's RSA public key.
    jwks = (await client.get("/.well-known/jwks.json")).json()

    # 4. Persist via LocalDeviceAuth.write (mirrors what the CLI does).
    secrets_path = tmp_path / "daemon-secrets.enc"
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    LocalDeviceAuth.write(
        secrets_path=secrets_path,
        device_id=pair_data["device_id"],
        device_token=pair_data["device_token"],
        central_iss=pair_data["central_iss"],
        auth_url="http://test.local",
        central_jwks=jwks,
    )
    assert secrets_path.exists()

    # 5. Simulate daemon boot OFFLINE: load + verify, no network
    auth = LocalDeviceAuth.load(secrets_path=secrets_path)
    assert auth.device_id == pair_data["device_id"]
    assert auth.user_email == "owner@example.com"
    assert auth.central_iss == "http://test.local"
    assert auth.expires_at > int(time.time())
    assert not auth.is_expired


@pytest.mark.asyncio
async def test_load_raises_when_not_paired(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "digitorn"))
    from digitorn.core.auth.local_device import LocalDeviceAuth, NotPaired

    fake_home = tmp_path / "empty-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    secrets_path = tmp_path / "absent.enc"
    with pytest.raises(NotPaired):
        LocalDeviceAuth.load(secrets_path=secrets_path)


@pytest.mark.asyncio
async def test_wrong_audience_rejected(app_and_client, tmp_path, monkeypatch):
    """A device_token issued for daemon A must NOT validate on daemon B
    (audience binding — central security guarantee)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "digitorn"))
    from digitorn.core.auth.local_device import (
        LocalDeviceAuth,
        InvalidDeviceToken,
    )

    _, client = app_and_client

    reg = await client.post("/auth/register", json={
        "username": "audtest",
        "password": "vveryltestpassword",
        "email": "aud@example.com",
    })
    access_token = reg.json()["access_token"]

    pair = await client.post(
        "/auth/devices/pair",
        json={"label": "First"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    pair_data = pair.json()
    jwks = (await client.get("/.well-known/jwks.json")).json()

    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Save the device_token but with a TAMPERED device_id in the
    # local file. The audience claim still says daemon-{original_id}
    # but our local LocalDeviceAuth.write stores a different one,
    # so verify(audience=daemon-{stored_id}) will fail.
    secrets_path = tmp_path / "secrets.enc"
    LocalDeviceAuth.write(
        secrets_path=secrets_path,
        device_id="bogus-device-id",            # ← tampered
        device_token=pair_data["device_token"], # ← real token, mismatched aud
        central_iss=pair_data["central_iss"],
        auth_url="http://test.local",
        central_jwks=jwks,
    )
    with pytest.raises(InvalidDeviceToken):
        LocalDeviceAuth.load(secrets_path=secrets_path)
