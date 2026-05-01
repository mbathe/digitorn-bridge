"""Mock external service APIs for credential E2E tests.

A single aiohttp server emulates several SaaS-style endpoints. Each
endpoint records the Authorization header it received so the test can
assert the agent's HTTP call carried the vault-injected credential.

Routes:

    GET  /stripe/v1/customers           — Stripe-style list
    POST /stripe/v1/charges             — create charge
    GET  /github/user                   — GitHub-style "/user"
    GET  /github/user/repos             — list repos
    POST /webhook/sign-verify           — accepts {payload, signature, alg}
                                           and returns {valid: true|false}
    GET  /__received                    — diagnostic: every auth header seen
    POST /__reset                       — reset

Run on port 9990.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections import defaultdict

from aiohttp import web

logger = logging.getLogger(__name__)
PORT = 9990

# Per-endpoint auth headers received - tests inspect this.
RECEIVED: list[dict] = []


async def _record(request: web.Request) -> None:
    RECEIVED.append({
        "path": request.path,
        "method": request.method,
        "auth": request.headers.get("Authorization", ""),
    })


async def stripe_list_customers(request: web.Request) -> web.Response:
    await _record(request)
    return web.json_response({
        "object": "list",
        "url": "/v1/customers",
        "has_more": False,
        "data": [
            {"id": "cus_001", "name": "Alice Doe",
             "email": "alice@example.com", "balance": 0},
            {"id": "cus_002", "name": "Bob Smith",
             "email": "bob@example.com", "balance": -1500},
            {"id": "cus_003", "name": "Charlie Roe",
             "email": "charlie@example.com", "balance": 25000},
        ],
    })


async def stripe_create_charge(request: web.Request) -> web.Response:
    await _record(request)
    body = await request.json() if request.body_exists else {}
    return web.json_response({
        "id": "ch_" + secrets.token_hex(8),
        "object": "charge",
        "amount": body.get("amount", 0),
        "currency": body.get("currency", "usd"),
        "status": "succeeded",
        "description": body.get("description", ""),
    })


async def github_me(request: web.Request) -> web.Response:
    await _record(request)
    return web.json_response({
        "login": "test-user",
        "id": 42,
        "name": "Test User",
        "email": "test@example.com",
        "public_repos": 7,
    })


async def github_repos(request: web.Request) -> web.Response:
    await _record(request)
    return web.json_response([
        {"name": "alpha", "full_name": "test-user/alpha", "private": False,
         "stargazers_count": 12, "language": "Python"},
        {"name": "beta", "full_name": "test-user/beta", "private": True,
         "stargazers_count": 0, "language": "Rust"},
        {"name": "gamma", "full_name": "test-user/gamma", "private": False,
         "stargazers_count": 245, "language": "TypeScript"},
    ])


async def basic_auth_proxy(request: web.Request) -> web.Response:
    """Endpoint that requires Basic auth. Returns 401 on missing/wrong."""
    await _record(request)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return web.json_response(
            {"error": "missing basic auth"}, status=401,
        )
    return web.json_response({
        "ok": True,
        "service": "internal-proxy",
        "message": "Authenticated.",
    })


async def webhook_sign_verify(request: web.Request) -> web.Response:
    """Verify HMAC signature on a payload. Test secret hardcoded;
    returns whether the supplied signature matches."""
    body = await request.json()
    payload = body.get("payload", "")
    sig = body.get("signature", "")
    secret = body.get("secret", "")
    expected = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()
    return web.json_response({
        "valid": hmac.compare_digest(sig, expected),
        "expected": expected[:16] + "...",
    })


async def cred_echo(request: web.Request) -> web.Response:
    """Generic endpoint that echos every header back. Used by the
    handler-coverage tests to assert that vault-injected auth /
    AWS / GCP / Azure / custom headers reach the wire."""
    await _record(request)
    return web.json_response({
        "ok": True,
        "received_headers": dict(request.headers),
        "url": str(request.url),
    })


async def aws_s3_list(request: web.Request) -> web.Response:
    await _record(request)
    aws_key = request.headers.get("X-Aws-Access-Key-Id", "")
    if not aws_key:
        return web.json_response(
            {"error": "missing AWS access key"}, status=401,
        )
    return web.json_response({
        "Buckets": [
            {"Name": "alpha-bucket", "CreationDate": "2024-01-01"},
            {"Name": "beta-bucket", "CreationDate": "2024-06-15"},
            {"Name": "gamma-bucket", "CreationDate": "2025-02-10"},
        ],
        "Owner": {"DisplayName": "test-owner"},
    })


async def gcp_token_exchange(request: web.Request) -> web.Response:
    """Mock GCP /token: validates the SA JSON arrived via header and
    returns an access_token if so."""
    await _record(request)
    sa_len = request.headers.get("X-Gcp-Sa-Length", "")
    if not sa_len or int(sa_len) < 50:
        return web.json_response(
            {"error": "missing or invalid SA"}, status=401,
        )
    return web.json_response({
        "access_token": "mock-gcp-token-AAAA",
        "expires_in": 3600,
        "token_type": "Bearer",
    })


async def azure_token(request: web.Request) -> web.Response:
    """Mock Azure /token endpoint: client_credentials grant."""
    await _record(request)
    tenant = request.headers.get("X-Azure-Tenant-Id", "")
    client = request.headers.get("X-Azure-Client-Id", "")
    if not (tenant and client):
        return web.json_response(
            {"error": "tenant/client missing"}, status=401,
        )
    return web.json_response({
        "access_token": "mock-azure-token-BBBB",
        "expires_in": 3600,
        "token_type": "Bearer",
    })


async def db_query(request: web.Request) -> web.Response:
    """Mock PostgREST-style: the http module sends the connection
    string as Bearer (because we mapped url -> token in the slot,
    or as X-Postgres-Url). Returns canned rows."""
    await _record(request)
    auth = request.headers.get("Authorization", "")
    if "postgres://" not in auth and not request.headers.get("X-Postgres-Url"):
        return web.json_response(
            {"error": "no postgres connection"}, status=401,
        )
    return web.json_response({
        "rows": [
            {"id": 1, "email": "alice@example.com"},
            {"id": 2, "email": "bob@example.com"},
            {"id": 3, "email": "charlie@example.com"},
        ],
        "count": 3,
    })


async def upload_receiver(request: web.Request) -> web.Response:
    """File-upload mock. Reads filename header + body length."""
    await _record(request)
    fname = request.headers.get("X-Vault-File-Name", "")
    body = await request.read()
    return web.json_response({
        "ok": True,
        "filename": fname,
        "received_bytes": len(body),
    })


async def custom_value_endpoint(request: web.Request) -> web.Response:
    await _record(request)
    val = request.headers.get("X-Vault-Custom", "")
    return web.json_response({
        "ok": bool(val),
        "received_value": val[:20] + "..." if len(val) > 20 else val,
    })


async def hmac_signed_endpoint(request: web.Request) -> web.Response:
    """Verifies an HMAC signature in X-Signature header against the
    body, using the test secret. Used for hmac_signing_secret e2e."""
    await _record(request)
    body = await request.read()
    sig = request.headers.get("X-Signature", "")
    secret = request.headers.get("X-Hmac-Secret", "")
    if not sig or not secret:
        return web.json_response(
            {"valid": False, "reason": "missing sig or secret"}, status=400,
        )
    expected = hmac.new(
        secret.encode(), body, hashlib.sha256,
    ).hexdigest()
    return web.json_response({
        "valid": hmac.compare_digest(sig, expected),
        "expected_first_16": expected[:16],
    })


async def received(request: web.Request) -> web.Response:
    return web.json_response({
        "received": RECEIVED,
        "count": len(RECEIVED),
    })


async def reset(request: web.Request) -> web.Response:
    RECEIVED.clear()
    return web.json_response({"ok": True})


def app() -> web.Application:
    a = web.Application()
    a.router.add_get("/stripe/v1/customers", stripe_list_customers)
    a.router.add_post("/stripe/v1/charges", stripe_create_charge)
    a.router.add_get("/github/user", github_me)
    a.router.add_get("/github/user/repos", github_repos)
    a.router.add_get("/proxy/me", basic_auth_proxy)
    a.router.add_post("/webhook/sign-verify", webhook_sign_verify)
    a.router.add_get("/cred-echo", cred_echo)
    a.router.add_get("/aws/s3/list-buckets", aws_s3_list)
    a.router.add_post("/gcp/token", gcp_token_exchange)
    a.router.add_post("/azure/token", azure_token)
    a.router.add_get("/db/users", db_query)
    a.router.add_post("/upload", upload_receiver)
    a.router.add_get("/custom", custom_value_endpoint)
    a.router.add_post("/hmac-protected", hmac_signed_endpoint)
    a.router.add_get("/__received", received)
    a.router.add_post("/__reset", reset)
    return a


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    web.run_app(app(), host="127.0.0.1", port=PORT)
