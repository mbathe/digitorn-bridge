"""AccountFeatures / admin endpoints + claim propagation tests."""

from __future__ import annotations

import jwt as pyjwt
import pytest


async def _bootstrap_admin_and_user(client) -> tuple[dict, dict]:
    """Login as the auto-bootstrapped 'admin' (created at lifespan
    start) + register a regular user. The bootstrap admin's
    credentials are the well-known defaults baked into AuthService —
    NOT for production but the right starting point for tests."""
    admin_login = await client.post("/auth/login", json={
        "username": "admin",
        "password": "admin1234admin",
    })
    assert admin_login.status_code == 200, admin_login.text
    admin = admin_login.json()
    target = (await client.post("/auth/register", json={
        "username": "target-user",
        "password": "vverytrgpassword",
        "email": "target@example.com",
    })).json()
    return admin, target


@pytest.mark.asyncio
async def test_features_default_when_no_row(app_and_client):
    _, client = app_and_client
    admin, _ = await _bootstrap_admin_and_user(client)

    # /me returns the implicit-free defaults
    r = await client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert r.status_code == 200
    feats = r.json()["features"]
    assert feats["plan_tier"] == "free"
    assert feats["cloud_enabled"] is False
    assert feats["self_host_enabled"] is True
    assert feats["max_paired_devices"] == 5


@pytest.mark.asyncio
async def test_admin_can_promote_user_to_pro(app_and_client):
    _, client = app_and_client
    admin, target = await _bootstrap_admin_and_user(client)

    # Admin upserts pro plan for the target user
    r = await client.put(
        f"/auth/admin/account-features/{target['user_id']}",
        json={
            "plan_tier": "pro",
            "cloud_enabled": True,
            "self_host_enabled": True,
            "cloud_token_quota_monthly": 5_000_000,
            "max_paired_devices": 100,
            "flags": {"beta_features": ["agent-mesh"]},
        },
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_tier"] == "pro"
    assert body["cloud_enabled"] is True
    assert body["max_paired_devices"] == 100
    assert body["flags"] == {"beta_features": ["agent-mesh"]}


@pytest.mark.asyncio
async def test_features_propagated_in_jwt_after_login(app_and_client):
    """After a feature row exists, the next login bakes it into the
    access token so daemons can read it OFFLINE without hitting /auth/me."""
    _, client = app_and_client
    admin, target = await _bootstrap_admin_and_user(client)

    # Promote target
    await client.put(
        f"/auth/admin/account-features/{target['user_id']}",
        json={
            "plan_tier": "enterprise",
            "cloud_enabled": True,
            "self_host_enabled": True,
            "cloud_token_quota_monthly": 0,  # unlimited
            "max_paired_devices": 0,
            "flags": {},
        },
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )

    # Target logs in fresh
    login = await client.post("/auth/login", json={
        "username": "target-user",
        "password": "vverytrgpassword",
    })
    assert login.status_code == 200
    access = login.json()["access_token"]

    # Decode without verifying — just inspect the claims a daemon
    # would read.
    claims = pyjwt.decode(access, options={"verify_signature": False})
    assert "features" in claims
    feats = claims["features"]
    assert feats["plan_tier"] == "enterprise"
    assert feats["cloud_enabled"] is True


@pytest.mark.asyncio
async def test_features_propagated_via_refresh(app_and_client):
    _, client = app_and_client
    admin, target = await _bootstrap_admin_and_user(client)

    await client.put(
        f"/auth/admin/account-features/{target['user_id']}",
        json={
            "plan_tier": "pro",
            "cloud_enabled": True,
            "self_host_enabled": True,
            "cloud_token_quota_monthly": 100_000,
            "max_paired_devices": 50,
            "flags": {},
        },
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )

    # Refresh exchanges the (free-claims) refresh token for a new
    # access token that should carry the upgraded features.
    refresh = (await client.post(
        "/auth/refresh",
        json={"refresh_token": target["refresh_token"]},
    )).json()
    claims = pyjwt.decode(refresh["access_token"], options={"verify_signature": False})
    assert claims["features"]["plan_tier"] == "pro"


@pytest.mark.asyncio
async def test_non_admin_cannot_set_features(app_and_client):
    _, client = app_and_client
    admin, target = await _bootstrap_admin_and_user(client)

    # Target (developer-role only) tries to upgrade themselves.
    r = await client.put(
        f"/auth/admin/account-features/{target['user_id']}",
        json={
            "plan_tier": "enterprise",
            "cloud_enabled": True,
            "self_host_enabled": True,
            "cloud_token_quota_monthly": 999_999_999,
            "max_paired_devices": 9999,
            "flags": {},
        },
        headers={"Authorization": f"Bearer {target['access_token']}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_delete_features_row(app_and_client):
    _, client = app_and_client
    admin, target = await _bootstrap_admin_and_user(client)

    # Set then delete
    await client.put(
        f"/auth/admin/account-features/{target['user_id']}",
        json={
            "plan_tier": "pro",
            "cloud_enabled": True,
            "self_host_enabled": True,
            "cloud_token_quota_monthly": 1000,
            "max_paired_devices": 10,
            "flags": {},
        },
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    r = await client.delete(
        f"/auth/admin/account-features/{target['user_id']}",
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert r.status_code == 204

    # /me on target now returns defaults again
    me_r = await client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {target['access_token']}"},
    )
    assert me_r.json()["features"]["plan_tier"] == "free"
