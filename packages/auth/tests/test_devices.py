"""Device pairing flow: pair, list, revalidate, revoke."""

from __future__ import annotations

import asyncio

import pytest


async def _register_and_get_token(client) -> tuple[str, str]:
    r = await client.post("/auth/register", json={
        "username": "device-user",
        "password": "vorrygoodpassword",
        "email": "device@example.com",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    return body["user_id"], body["access_token"]


@pytest.mark.asyncio
async def test_pair_requires_auth(app_and_client):
    _, client = app_and_client
    r = await client.post("/auth/devices/pair", json={"label": "Mac"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_pair_returns_device_token(app_and_client):
    _, client = app_and_client
    user_id, access = await _register_and_get_token(client)

    r = await client.post(
        "/auth/devices/pair",
        json={"label": "Paul-MacBook"},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["device_id"]
    assert body["device_token"]
    assert body["expires_at"] > 0
    assert body["central_iss"] == "http://test.local"


@pytest.mark.asyncio
async def test_list_devices_returns_paired(app_and_client):
    _, client = app_and_client
    _, access = await _register_and_get_token(client)
    headers = {"Authorization": f"Bearer {access}"}

    await client.post("/auth/devices/pair", json={"label": "Mac"}, headers=headers)
    await client.post("/auth/devices/pair", json={"label": "Pi"}, headers=headers)

    r = await client.get("/auth/devices", headers=headers)
    assert r.status_code == 200, r.text
    devices = r.json()
    assert len(devices) == 2
    labels = sorted(d["label"] for d in devices)
    assert labels == ["Mac", "Pi"]
    assert all(d["is_active"] for d in devices)


@pytest.mark.asyncio
async def test_revalidate_with_valid_token(app_and_client):
    _, client = app_and_client
    _, access = await _register_and_get_token(client)
    headers = {"Authorization": f"Bearer {access}"}

    pair = (await client.post(
        "/auth/devices/pair", json={"label": "Mac"}, headers=headers,
    )).json()
    device_id = pair["device_id"]
    device_token = pair["device_token"]

    # Daemon would call this hourly. No fresh access token, just the device token.
    r = await client.post(
        f"/auth/devices/{device_id}/revalidate",
        headers={"Authorization": f"Bearer {device_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True


@pytest.mark.asyncio
async def test_revalidate_rolling_refresh(app_and_client):
    """When the device_token expires inside the rolling threshold,
    the server returns a fresh one. Conftest sets ttl=60 / threshold=30
    so a paired token (ttl=60s remaining) expires within 30s and
    triggers refresh on the very first revalidate."""
    _, client = app_and_client
    _, access = await _register_and_get_token(client)
    headers = {"Authorization": f"Bearer {access}"}

    pair = (await client.post(
        "/auth/devices/pair", json={"label": "Mac"}, headers=headers,
    )).json()
    # Wait long enough for the issued token to slip inside the
    # rolling-refresh window. ttl=60, threshold=30 → refresh kicks
    # in any time after the token is at least (ttl-threshold)=30s old.
    await asyncio.sleep(31)

    r = await client.post(
        f"/auth/devices/{pair['device_id']}/revalidate",
        headers={"Authorization": f"Bearer {pair['device_token']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert body["renewed_token"] is not None
    assert body["renewed_expires_at"] > 0
    assert body["renewed_token"] != pair["device_token"]


@pytest.mark.asyncio
async def test_revalidate_rejects_revoked_device(app_and_client):
    _, client = app_and_client
    _, access = await _register_and_get_token(client)
    headers = {"Authorization": f"Bearer {access}"}

    pair = (await client.post(
        "/auth/devices/pair", json={"label": "Mac"}, headers=headers,
    )).json()

    # Revoke from the dashboard
    r = await client.delete(
        f"/auth/devices/{pair['device_id']}",
        headers=headers,
    )
    assert r.status_code == 204

    # Daemon's next revalidate must return valid=false
    r = await client.post(
        f"/auth/devices/{pair['device_id']}/revalidate",
        headers={"Authorization": f"Bearer {pair['device_token']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["revoked_reason"] == "device_revoked"


@pytest.mark.asyncio
async def test_revalidate_rejects_invalid_token(app_and_client):
    _, client = app_and_client
    _, access = await _register_and_get_token(client)

    pair = (await client.post(
        "/auth/devices/pair",
        json={"label": "Mac"},
        headers={"Authorization": f"Bearer {access}"},
    )).json()

    r = await client.post(
        f"/auth/devices/{pair['device_id']}/revalidate",
        headers={"Authorization": "Bearer not.a.valid.jwt"},
    )
    assert r.status_code == 200
    assert r.json()["valid"] is False


@pytest.mark.asyncio
async def test_revalidate_rejects_access_token(app_and_client):
    """An access token (from /auth/login) MUST NOT be accepted on
    /revalidate - only device tokens have the right scope."""
    _, client = app_and_client
    _, access = await _register_and_get_token(client)

    pair = (await client.post(
        "/auth/devices/pair",
        json={"label": "Mac"},
        headers={"Authorization": f"Bearer {access}"},
    )).json()

    r = await client.post(
        f"/auth/devices/{pair['device_id']}/revalidate",
        headers={"Authorization": f"Bearer {access}"},  # access, not device
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["revoked_reason"] == "wrong_token_type"


@pytest.mark.asyncio
async def test_user_cannot_revoke_other_users_device(app_and_client):
    _, client = app_and_client

    # User A pairs a device
    user_a = (await client.post("/auth/register", json={
        "username": "user-a", "password": "passsword-a-12345", "email": "a@x.com",
    })).json()
    pair = (await client.post(
        "/auth/devices/pair",
        json={"label": "Mac-A"},
        headers={"Authorization": f"Bearer {user_a['access_token']}"},
    )).json()

    # User B tries to revoke A's device
    user_b = (await client.post("/auth/register", json={
        "username": "user-b", "password": "passsword-b-12345", "email": "b@x.com",
    })).json()
    r = await client.delete(
        f"/auth/devices/{pair['device_id']}",
        headers={"Authorization": f"Bearer {user_b['access_token']}"},
    )
    assert r.status_code == 404

    # And listing returns nothing for B
    r = await client.get(
        "/auth/devices",
        headers={"Authorization": f"Bearer {user_b['access_token']}"},
    )
    assert r.status_code == 200
    assert r.json() == []
