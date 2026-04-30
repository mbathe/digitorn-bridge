"""User-owned sessions: list + revoke per id, ownership enforcement."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_sessions_returns_active_refresh_tokens(app_and_client):
    _, client = app_and_client

    user = (await client.post(
        "/auth/register",
        json={"username": "sessuser", "password": "vverytestpassword", "email": "s@x.com"},
        headers={"User-Agent": "Mozilla/5.0 (Test)"},
    )).json()
    access = user["access_token"]

    # Two more logins from different "browsers" (different UA strings)
    await client.post(
        "/auth/login",
        json={"username": "sessuser", "password": "vverytestpassword"},
        headers={"User-Agent": "Mozilla/5.0 (Mac Safari)"},
    )
    await client.post(
        "/auth/login",
        json={"username": "sessuser", "password": "vverytestpassword"},
        headers={"User-Agent": "Mozilla/5.0 (Pixel Chrome)"},
    )

    r = await client.get(
        "/auth/sessions",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) >= 3
    uas = {row.get("device_info", "") for row in rows}
    assert "Mozilla/5.0 (Test)" in uas
    assert "Mozilla/5.0 (Mac Safari)" in uas
    assert "Mozilla/5.0 (Pixel Chrome)" in uas


@pytest.mark.asyncio
async def test_revoke_session_removes_from_list(app_and_client):
    _, client = app_and_client
    user = (await client.post(
        "/auth/register",
        json={"username": "revsess", "password": "vverytestpassword"},
        headers={"User-Agent": "test-ua"},
    )).json()
    access = user["access_token"]

    rows = (await client.get(
        "/auth/sessions",
        headers={"Authorization": f"Bearer {access}"},
    )).json()
    assert len(rows) == 1
    target_id = rows[0]["id"]

    r = await client.delete(
        f"/auth/sessions/{target_id}",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code == 204

    rows = (await client.get(
        "/auth/sessions",
        headers={"Authorization": f"Bearer {access}"},
    )).json()
    assert all(row["id"] != target_id for row in rows)


@pytest.mark.asyncio
async def test_user_cannot_revoke_other_users_session(app_and_client):
    _, client = app_and_client

    a = (await client.post(
        "/auth/register",
        json={"username": "alice-sess", "password": "vverytestpassword"},
    )).json()
    b = (await client.post(
        "/auth/register",
        json={"username": "bob-sess", "password": "vverytestpassword"},
    )).json()

    a_sessions = (await client.get(
        "/auth/sessions",
        headers={"Authorization": f"Bearer {a['access_token']}"},
    )).json()
    a_session_id = a_sessions[0]["id"]

    # Bob tries to revoke Alice's session
    r = await client.delete(
        f"/auth/sessions/{a_session_id}",
        headers={"Authorization": f"Bearer {b['access_token']}"},
    )
    assert r.status_code == 404

    # Alice's session is still active
    a_sessions_after = (await client.get(
        "/auth/sessions",
        headers={"Authorization": f"Bearer {a['access_token']}"},
    )).json()
    assert any(s["id"] == a_session_id for s in a_sessions_after)


@pytest.mark.asyncio
async def test_sessions_endpoint_requires_auth(app_and_client):
    _, client = app_and_client
    r = await client.get("/auth/sessions")
    assert r.status_code == 401
