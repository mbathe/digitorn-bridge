"""Admin endpoints for role management."""

from __future__ import annotations

import pytest


async def _bootstrap_admin_and_user(client) -> tuple[dict, dict]:
    admin_login = await client.post("/auth/login", json={
        "username": "admin", "password": "admin1234admin",
    })
    assert admin_login.status_code == 200, admin_login.text
    admin = admin_login.json()
    target = (await client.post("/auth/register", json={
        "username": "rolee", "password": "vverytrgpassword",
        "email": "rolee@example.com",
    })).json()
    return admin, target


@pytest.mark.asyncio
async def test_admin_lists_builtin_roles(app_and_client):
    _, client = app_and_client
    admin, _ = await _bootstrap_admin_and_user(client)
    r = await client.get(
        "/auth/admin/roles",
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    names = {row["name"] for row in rows}
    # Built-ins seeded at first lifespan start.
    assert "admin" in names
    assert "developer" in names


@pytest.mark.asyncio
async def test_admin_can_assign_and_revoke_role(app_and_client):
    _, client = app_and_client
    admin, target = await _bootstrap_admin_and_user(client)
    h = {"Authorization": f"Bearer {admin['access_token']}"}

    # Target starts without 'admin' role.
    r = await client.get(f"/auth/admin/users/{target['user_id']}", headers=h)
    assert r.status_code == 200
    assert "admin" not in r.json()["roles"]

    # Assign 'admin'.
    r = await client.post(
        f"/auth/admin/users/{target['user_id']}/roles/admin", headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assigned"] is True
    assert body["already"] is False

    # Idempotent: second call says already.
    r = await client.post(
        f"/auth/admin/users/{target['user_id']}/roles/admin", headers=h,
    )
    assert r.status_code == 200
    assert r.json()["already"] is True

    # User now carries the role.
    r = await client.get(f"/auth/admin/users/{target['user_id']}", headers=h)
    assert "admin" in r.json()["roles"]

    # Revoke.
    r = await client.delete(
        f"/auth/admin/users/{target['user_id']}/roles/admin", headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["rows_affected"] == 1

    # Gone.
    r = await client.get(f"/auth/admin/users/{target['user_id']}", headers=h)
    assert "admin" not in r.json()["roles"]


@pytest.mark.asyncio
async def test_assign_unknown_role_returns_404(app_and_client):
    _, client = app_and_client
    admin, target = await _bootstrap_admin_and_user(client)
    h = {"Authorization": f"Bearer {admin['access_token']}"}
    r = await client.post(
        f"/auth/admin/users/{target['user_id']}/roles/nope-role", headers=h,
    )
    assert r.status_code == 404
    assert "role_not_found" in r.json()["detail"]


@pytest.mark.asyncio
async def test_assign_to_unknown_user_returns_404(app_and_client):
    _, client = app_and_client
    admin, _ = await _bootstrap_admin_and_user(client)
    h = {"Authorization": f"Bearer {admin['access_token']}"}
    r = await client.post(
        "/auth/admin/users/no-such-user/roles/admin", headers=h,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_non_admin_cannot_assign_role(app_and_client):
    _, client = app_and_client
    _, target = await _bootstrap_admin_and_user(client)
    h = {"Authorization": f"Bearer {target['access_token']}"}
    r = await client.post(
        f"/auth/admin/users/{target['user_id']}/roles/admin", headers=h,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_revoke_role_user_didnt_have_is_idempotent(app_and_client):
    _, client = app_and_client
    admin, target = await _bootstrap_admin_and_user(client)
    h = {"Authorization": f"Bearer {admin['access_token']}"}
    r = await client.delete(
        f"/auth/admin/users/{target['user_id']}/roles/admin", headers=h,
    )
    assert r.status_code == 200
    assert r.json()["rows_affected"] == 0
