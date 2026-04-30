"""Token revocation flow + list endpoint."""

from __future__ import annotations

import jwt as pyjwt
import pytest


@pytest.mark.asyncio
async def test_logout_persists_revocation(app_and_client):
    _, client = app_and_client
    # Bootstrap admin (auto-created at lifespan), so we can list revocations.
    admin_login = await client.post("/auth/login", json={
        "username": "admin", "password": "admin1234admin",
    })
    admin = admin_login.json()

    # Register a regular user, log them out (which should revoke the access jti)
    user = (await client.post("/auth/register", json={
        "username": "logouter", "password": "vverytestpassword", "email": "lo@x.com",
    })).json()
    access = user["access_token"]
    claims = pyjwt.decode(access, options={"verify_signature": False})
    jti = claims["jti"]

    r = await client.post(
        "/auth/logout",
        json={"refresh_token": user["refresh_token"]},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code == 204

    # Admin lists revocations - the just-logged-out jti should appear
    r = await client.get(
        "/auth/admin/revocations",
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert any(row["jti"] == jti and row["reason"] == "user_logout" for row in rows)


@pytest.mark.asyncio
async def test_admin_revoke_endpoint(app_and_client):
    _, client = app_and_client
    admin = (await client.post("/auth/login", json={
        "username": "admin", "password": "admin1234admin",
    })).json()
    target = (await client.post("/auth/register", json={
        "username": "victim", "password": "vverytestpassword", "email": "v@x.com",
    })).json()
    target_claims = pyjwt.decode(target["access_token"], options={"verify_signature": False})

    r = await client.post(
        "/auth/admin/revoke",
        json={
            "jti": target_claims["jti"],
            "user_id": target["user_id"],
            "expires_at": target_claims["exp"],
            "reason": "compromised",
        },
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert r.status_code == 204

    rows = (await client.get(
        "/auth/admin/revocations",
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )).json()
    assert any(
        row["jti"] == target_claims["jti"] and row["reason"] == "compromised"
        for row in rows
    )


@pytest.mark.asyncio
async def test_revoke_all_for_user_blocks_refresh(app_and_client):
    _, client = app_and_client
    admin = (await client.post("/auth/login", json={
        "username": "admin", "password": "admin1234admin",
    })).json()
    target = (await client.post("/auth/register", json={
        "username": "manyrefresh", "password": "vverytestpassword",
    })).json()

    # Log in twice more to simulate "user has multiple sessions"
    for _ in range(2):
        await client.post("/auth/login", json={
            "username": "manyrefresh", "password": "vverytestpassword",
        })

    r = await client.post(
        f"/auth/admin/revoke-all/{target['user_id']}",
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["refresh_tokens_revoked"] >= 1

    # The original refresh token should now be rejected
    r = await client.post("/auth/refresh", json={"refresh_token": target["refresh_token"]})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_non_admin_cannot_list_revocations(app_and_client):
    _, client = app_and_client
    user = (await client.post("/auth/register", json={
        "username": "noseyuser", "password": "vverytestpassword",
    })).json()
    r = await client.get(
        "/auth/admin/revocations",
        headers={"Authorization": f"Bearer {user['access_token']}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_revocations_filtered_by_since(app_and_client):
    _, client = app_and_client
    admin = (await client.post("/auth/login", json={
        "username": "admin", "password": "admin1234admin",
    })).json()
    target = (await client.post("/auth/register", json={
        "username": "filtersince", "password": "vverytestpassword",
    })).json()
    claims = pyjwt.decode(target["access_token"], options={"verify_signature": False})

    await client.post(
        "/auth/admin/revoke",
        json={
            "jti": claims["jti"],
            "user_id": target["user_id"],
            "expires_at": claims["exp"],
            "reason": "audit_test",
        },
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )

    # since=now+1h => no rows newer than that
    import time
    future = time.time() + 3600
    r = await client.get(
        f"/auth/admin/revocations?since={future}",
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert r.status_code == 200
    assert r.json() == []
