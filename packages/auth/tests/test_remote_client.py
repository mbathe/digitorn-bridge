"""End-to-end tests for the RemoteAuthClient + FastAPI integration.

Spins up TWO ASGI apps in the same process:
  - the auth service (issues tokens)
  - a "consumer" daemon that uses RemoteAuthMiddleware

Validates that a token issued by the central is correctly verified by
the consumer, and that a token signed by an unrelated key is rejected.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from httpx import AsyncClient, ASGITransport

from digitorn_auth.client import RemoteAuthClient
from digitorn_auth.fastapi import (
    RemoteAuthMiddleware,
    install_remote_auth,
    require_user,
)


@pytest_asyncio.fixture
async def auth_and_consumer(app_and_client, tmp_path):
    """Build an auth-service ASGI client AND a consumer FastAPI app
    that uses RemoteAuthMiddleware to verify tokens issued by the auth
    service. Both run in-process via ASGI transport so the consumer
    can fetch the central's JWKS without going through the network.
    """
    _, auth_client = app_and_client

    consumer = FastAPI()

    # The consumer's RemoteAuthClient needs to fetch JWKS from the
    # auth service. Both apps live in-process; we patch httpx to
    # route requests addressed at "http://test.local" to the auth
    # service's ASGITransport.
    import httpx
    original_async_client = httpx.AsyncClient

    auth_app = app_and_client[0]

    class PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", ASGITransport(app=auth_app))
            kwargs.setdefault("base_url", "http://test.local")
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = PatchedAsyncClient
    try:
        consumer.add_middleware(
            RemoteAuthMiddleware,
            issuer="http://test.local",
        )

        @consumer.get("/who-am-i")
        async def who(request: Request):
            return {
                "user_id": request.state.user_id,
                "email": request.state.user_email,
                "roles": request.state.roles,
            }

        @consumer.get("/protected")
        async def protected(claims=Depends(require_user)):
            return {"hello": claims.user_id}

        @consumer.get("/health")
        async def health():
            return {"ok": True}

        # First request will lazy-install the RemoteAuthClient.
        # Use ASGITransport so we don't actually hit the network.
        async with consumer.router.lifespan_context(consumer):
            transport = ASGITransport(app=consumer)
            async with AsyncClient(
                transport=transport, base_url="http://consumer.local",
            ) as cclient:
                yield auth_client, cclient
    finally:
        httpx.AsyncClient = original_async_client


@pytest.mark.asyncio
async def test_consumer_accepts_central_token(auth_and_consumer):
    auth_client, consumer = auth_and_consumer

    reg = await auth_client.post("/auth/register", json={
        "username": "remote-user",
        "password": "vveryltestpassword",
        "email": "remote@example.com",
    })
    access_token = reg.json()["access_token"]

    r = await consumer.get(
        "/who-am-i", headers={"Authorization": f"Bearer {access_token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"]
    assert body["email"] == "remote@example.com"


@pytest.mark.asyncio
async def test_consumer_rejects_no_token(auth_and_consumer):
    _, consumer = auth_and_consumer
    r = await consumer.get("/who-am-i")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_consumer_rejects_garbage_token(auth_and_consumer):
    _, consumer = auth_and_consumer
    r = await consumer.get(
        "/who-am-i", headers={"Authorization": "Bearer not.a.real.jwt"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_health_path_bypasses_auth(auth_and_consumer):
    _, consumer = auth_and_consumer
    r = await consumer.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_require_user_dependency(auth_and_consumer):
    auth_client, consumer = auth_and_consumer

    reg = await auth_client.post("/auth/register", json={
        "username": "dep-user",
        "password": "vveryltestpassword",
    })
    access_token = reg.json()["access_token"]

    r = await consumer.get(
        "/protected", headers={"Authorization": f"Bearer {access_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["hello"]


@pytest.mark.asyncio
async def test_token_from_other_signer_rejected(auth_and_consumer, tmp_path):
    """A token signed by a DIFFERENT RSA key (not in the consumer's
    cached JWKS) MUST be rejected — that's the whole security model."""
    _, consumer = auth_and_consumer

    from digitorn_auth.jwt import JWTService
    rogue = JWTService(
        algorithm="RS256",
        access_ttl=3600,
        refresh_ttl=3600,
        private_key_path=tmp_path / "rogue-priv.pem",
        public_key_path=tmp_path / "rogue-pub.pem",
        kid="rogue-kid",
    )
    forged = rogue.generate_access_token(
        user_id="attacker", email="evil@x.com",
    )

    r = await consumer.get(
        "/who-am-i", headers={"Authorization": f"Bearer {forged}"},
    )
    assert r.status_code == 401
