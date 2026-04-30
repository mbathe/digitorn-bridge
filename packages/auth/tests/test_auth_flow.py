"""End-to-end auth flow: register, login, refresh, /me, logout."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_register_then_login(app_and_client):
    _, client = app_and_client

    r = await client.post("/auth/register", json={
        "username": "alice",
        "password": "correct horse battery",
        "email": "alice@example.com",
        "display_name": "Alice",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"]
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["email"] == "alice@example.com"

    # Same credentials must log in
    r = await client.post("/auth/login", json={
        "username": "alice",
        "password": "correct horse battery",
    })
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == body["user_id"]


@pytest.mark.asyncio
async def test_login_with_email_alias(app_and_client):
    _, client = app_and_client

    await client.post("/auth/register", json={
        "username": "bob",
        "password": "passw0rd-passw0rd",
        "email": "bob@example.com",
    })
    r = await client.post("/auth/login", json={
        "email": "bob@example.com",
        "password": "passw0rd-passw0rd",
    })
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(app_and_client):
    _, client = app_and_client

    await client.post("/auth/register", json={
        "username": "carol",
        "password": "real-password-1",
        "email": "carol@example.com",
    })
    r = await client.post("/auth/login", json={
        "username": "carol",
        "password": "wrong-password",
    })
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_register_rejects_duplicate_email(app_and_client):
    _, client = app_and_client

    r = await client.post("/auth/register", json={
        "username": "dave",
        "password": "passw0rd-passw0rd",
        "email": "dup@example.com",
    })
    assert r.status_code == 200

    r = await client.post("/auth/register", json={
        "username": "dave2",
        "password": "passw0rd-passw0rd",
        "email": "dup@example.com",
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_refresh_rotates_tokens(app_and_client):
    _, client = app_and_client

    reg = await client.post("/auth/register", json={
        "username": "eve",
        "password": "ssomething-strong",
    })
    refresh = reg.json()["refresh_token"]

    r = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200, r.text
    new_access = r.json()["access_token"]
    new_refresh = r.json()["refresh_token"]
    assert new_access
    # The OLD refresh must NOT work anymore (one-shot rotation)
    r2 = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert r2.status_code == 401
    # The NEW one DOES
    r3 = await client.post("/auth/refresh", json={"refresh_token": new_refresh})
    assert r3.status_code == 200


@pytest.mark.asyncio
async def test_me_endpoint_requires_token(app_and_client):
    _, client = app_and_client

    r = await client.get("/auth/me")
    assert r.status_code == 401

    reg = await client.post("/auth/register", json={
        "username": "frank",
        "password": "vveryltestpassword",
        "email": "frank@example.com",
    })
    access = reg.json()["access_token"]
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200
    assert r.json()["email"] == "frank@example.com"


@pytest.mark.asyncio
async def test_health_endpoint(app_and_client):
    _, client = app_and_client
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == "digitorn-auth"


@pytest.mark.asyncio
async def test_well_known_endpoints(app_and_client):
    _, client = app_and_client

    r = await client.get("/.well-known/openid-configuration")
    assert r.status_code == 200
    body = r.json()
    assert body["issuer"] == "http://test.local"
    assert "jwks_uri" in body

    r = await client.get("/.well-known/jwks.json")
    assert r.status_code == 200
    body = r.json()
    # RS256 default: at least one key with kid + n/e (RSA public).
    # HS256: empty list. Accept both — the test tmp_path fixture
    # may end up in either mode depending on what envvars survive.
    keys = body.get("keys", [])
    if keys:
        k = keys[0]
        assert k["kty"] == "RSA"
        assert k["alg"] == "RS256"
        assert k["kid"]
        assert k["n"] and k["e"]
