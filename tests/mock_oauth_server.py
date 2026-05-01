"""Mock OAuth 2.0 provider for end-to-end testing.

Implements the minimum subset of the auth-code grant + refresh +
revocation flow so the daemon's `oauth_flow.py` and
`oauth_refresh_loop.py` can be exercised without registering with a
real provider (Google, GitHub, …).

Endpoints:

    GET  /authorize                # show consent UI (auto-approves)
    POST /token                    # exchange code -> access+refresh
    POST /revoke                   # revoke a token

Internal endpoints (test fixtures):

    POST /__configure              # set the simulated user info
    GET  /__state                  # snapshot of issued tokens

Run:

    py -3.12 tests/mock_oauth_server.py
"""
from __future__ import annotations

import base64
import logging
import secrets
from urllib.parse import urlencode, urlparse, parse_qs

from aiohttp import web

logger = logging.getLogger(__name__)

PORT = 9998

# In-memory state.
PENDING_CODES: dict[str, dict] = {}     # code -> {scope, redirect_uri, state}
ISSUED_TOKENS: dict[str, dict] = {}     # access_token -> {refresh_token, expires_at}
REVOKED: set[str] = set()


async def authorize(request: web.Request) -> web.Response:
    """Implements the consent step. We auto-approve and redirect to
    the configured redirect_uri with a fresh code + state echo.
    """
    qs = request.rel_url.query
    redirect_uri = qs.get("redirect_uri", "")
    state = qs.get("state", "")
    scope = qs.get("scope", "")
    if not redirect_uri:
        return web.Response(status=400, text="missing redirect_uri")
    code = "code-" + secrets.token_urlsafe(12)
    PENDING_CODES[code] = {"scope": scope, "redirect_uri": redirect_uri,
                           "state": state}
    sep = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{sep}{urlencode({'code': code, 'state': state})}"
    logger.info("mock_oauth_authorize -> %s", target)
    return web.Response(status=302, headers={"Location": target})


async def token(request: web.Request) -> web.Response:
    """Exchange code for access_token (auth-code grant) OR exchange
    refresh_token for new access_token (refresh grant)."""
    # Body may be x-www-form-urlencoded.
    form = await request.post()
    grant = form.get("grant_type", "")
    if grant == "authorization_code":
        code = form.get("code", "")
        rec = PENDING_CODES.pop(code, None)
        if rec is None:
            return web.json_response({"error": "invalid_grant"}, status=400)
        ak = "ya29.mock-" + secrets.token_urlsafe(12)
        rt = "1//mock-rt-" + secrets.token_urlsafe(12)
        ISSUED_TOKENS[ak] = {"refresh_token": rt, "scope": rec["scope"]}
        return web.json_response({
            "access_token": ak,
            "refresh_token": rt,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": rec["scope"],
        })
    if grant == "refresh_token":
        rt = form.get("refresh_token", "")
        # Find the original record by refresh_token.
        original = None
        original_ak = None
        for ak, r in ISSUED_TOKENS.items():
            if r.get("refresh_token") == rt:
                original = r
                original_ak = ak
                break
        if original is None:
            return web.json_response({"error": "invalid_grant"}, status=400)
        # Issue a new access_token (mock providers often rotate the
        # refresh_token too - we keep it stable for simplicity).
        new_ak = "ya29.mock-refresh-" + secrets.token_urlsafe(12)
        ISSUED_TOKENS[new_ak] = original
        if original_ak in ISSUED_TOKENS:
            del ISSUED_TOKENS[original_ak]
        return web.json_response({
            "access_token": new_ak,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": original.get("scope", ""),
        })
    return web.json_response({"error": "unsupported_grant_type"}, status=400)


async def revoke(request: web.Request) -> web.Response:
    """Token revocation. Marks the token revoked + drops from issued."""
    form = await request.post()
    tok = form.get("token", "") or request.rel_url.query.get("token", "")
    if not tok:
        return web.Response(status=400, text="missing token")
    REVOKED.add(tok)
    if tok in ISSUED_TOKENS:
        del ISSUED_TOKENS[tok]
    logger.info("mock_oauth_revoked token=%s", tok[:20])
    return web.Response(status=200, text="OK")


async def state(request: web.Request) -> web.Response:
    return web.json_response({
        "pending_codes": list(PENDING_CODES.keys()),
        "issued_tokens": [
            {"access_token": k[:20] + "...",
             "scope": v.get("scope", "")}
            for k, v in ISSUED_TOKENS.items()
        ],
        "revoked_count": len(REVOKED),
    })


def app() -> web.Application:
    a = web.Application()
    a.router.add_get("/authorize", authorize)
    a.router.add_post("/token", token)
    a.router.add_post("/revoke", revoke)
    a.router.add_get("/__state", state)
    return a


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    web.run_app(app(), host="127.0.0.1", port=PORT)
