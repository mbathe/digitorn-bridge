"""OIDC discovery + JWKS endpoints.

Exposes the public material that daemons (and any other OIDC client)
need to verify JWTs offline.

The JWKS is built once at lifespan startup (``server.py`` stashes it
on ``app.state.jwks``) so each request just dumps the cached dict —
no per-request key serialisation cost.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from digitorn_auth.config import get_settings

router = APIRouter(tags=["oidc"])


@router.get("/.well-known/openid-configuration")
async def openid_configuration():
    """OIDC discovery doc.

    Daemons can hit this to discover all the auth endpoints
    (login, JWKS, userinfo, …) instead of hardcoding URLs.
    Standard OIDC pattern, works with every OIDC client library.
    """
    settings = get_settings()
    base = settings.issuer.rstrip("/")
    return {
        "issuer": settings.issuer,
        "authorization_endpoint": f"{base}/auth/login",
        "token_endpoint": f"{base}/auth/refresh",
        "userinfo_endpoint": f"{base}/auth/me",
        "jwks_uri": f"{base}/.well-known/jwks.json",
        "registration_endpoint": f"{base}/auth/register",
        "response_types_supported": ["code", "token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": [settings.jwt_algorithm],
        "scopes_supported": ["openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic", "client_secret_post",
        ],
    }


@router.get("/.well-known/jwks.json")
async def jwks(request: Request):
    """Public key set for token verification.

    For HS256 deployments this returns ``{"keys": []}`` — there is no
    public key to expose, the secret is shared out of band.
    For RS256 (default) this returns the active RSA public key in JWK
    format with its ``kid`` so daemons can pick it up automatically.
    """
    cached = getattr(request.app.state, "jwks", None)
    if cached is not None:
        return cached
    return {"keys": []}
