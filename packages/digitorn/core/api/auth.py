"""Auth API endpoints - login, register, token refresh, user management.

All endpoints return JSON. Token-based authentication via Bearer header.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, FastAPI
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

class LoginRequest(BaseModel):
    username: str | None = Field(default=None, description="Username (for local/LDAP)")
    email: str | None = Field(default=None, description="Email (alternative to username)")
    password: str = Field(..., description="Password")
    provider: str | None = Field(default=None, description="Auth provider (default: local)")

class RegisterRequest(BaseModel):
    model_config = {"populate_by_name": True, "extra": "ignore"}

    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=12, max_length=128)
    email: str | None = Field(default=None)
    # BUG-060: older SDKs send `name` instead of `display_name`. Accept
    # either via the ``name`` alias so the field stops silently vanishing
    # from the stored profile.
    display_name: str | None = Field(default=None, alias="name")

class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., description="Refresh token to exchange")


class LogoutRequest(BaseModel):
    """Logout body - everything is optional.

    Callers typically POST an empty body and rely on the Authorization
    header to identify the access token to revoke. Older SDKs that pass
    a refresh_token keep working; both get revoked atomically.
    """

    refresh_token: str | None = Field(default=None)

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int = 0
    user_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    roles: list[str] = []
    permissions: list[str] = []
    is_admin: bool = False

class UserResponse(BaseModel):
    user_id: str
    email: str | None = None
    display_name: str | None = None
    roles: list[str] = []
    permissions: list[str] = []
    is_admin: bool = False
    # Extra UI metadata - avatar_url / created_at / last_seen_at
    # come from the User DB row (populated by the profile store).
    avatar_url: str | None = None
    created_at: str | None = None
    last_seen_at: str | None = None
    phone: str | None = None


def _is_admin(roles: list[str] | None, permissions: list[str] | None) -> bool:
    return "admin" in (roles or []) or "*" in (permissions or [])

class ErrorResponse(BaseModel):
    error: str

def _get_auth(request: Request):
    """Get AuthService from app state. Raises 503 if auth is disabled."""
    auth = getattr(request.app.state, "auth_service", None)
    if auth is None:
        from starlette.exceptions import HTTPException
        raise HTTPException(
            status_code=503,
            detail="Authentication is disabled on this daemon",
        )
    return auth

@router.post("/login", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def login(body: LoginRequest, request: Request):
    """Authenticate and receive JWT tokens."""
    auth = _get_auth(request)

    credentials = {"password": body.password}
    if body.username:
        credentials["username"] = body.username
    if body.email:
        credentials["email"] = body.email
    client = getattr(request, "client", None)
    credentials["ip"] = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or getattr(client, "host", "") or ""
    )

    result = await auth.login(credentials, provider=body.provider)

    if not result.success:
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": result.error})

    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        user_id=result.user_id,
        email=result.email,
        display_name=result.display_name,
        roles=result.roles or [],
        permissions=result.permissions or [],
        is_admin=_is_admin(result.roles, result.permissions),
    )

@router.post("/register", response_model=TokenResponse, responses={400: {"model": ErrorResponse}})
async def register(body: RegisterRequest, request: Request):
    """Register a new local user and receive JWT tokens."""
    auth = _get_auth(request)

    result = await auth.register(
        username=body.username,
        password=body.password,
        email=body.email,
        display_name=body.display_name,
    )

    if not result.success:
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=400, content={"error": result.error})

    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        user_id=result.user_id,
        email=result.email,
        display_name=result.display_name,
        roles=result.roles or [],
        permissions=result.permissions or [],
        is_admin=_is_admin(result.roles, result.permissions),
    )

@router.post("/refresh", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def refresh(body: RefreshRequest, request: Request):
    """Exchange a refresh token for a new access token."""
    auth = _get_auth(request)

    result = await auth.refresh(body.refresh_token)

    if not result.success:
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": result.error})

    return TokenResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        user_id=result.user_id,
        roles=result.roles or [],
        permissions=result.permissions or [],
        is_admin=_is_admin(result.roles, result.permissions),
    )

@router.post("/logout")
async def logout(request: Request, body: LogoutRequest | None = None):
    """Revoke the caller's session.

    Accepts an empty body. The access token is taken from the
    ``Authorization: Bearer`` header (the same place the middleware
    found it to let the request through), so logout is a single call
    from the client with no payload required.
    """
    try:
        auth = _get_auth(request)
        refresh_token = body.refresh_token if body else None
        access_token: str | None = None
        
        # Log le header brut pour debug
        header = request.headers.get("authorization") or ""
        logger.debug(f"Authorization header (raw): {repr(header)}")  # DEBUG
        
        header = header.strip()  
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
            logger.debug(f"Extracted token: {repr(token)}")  # DEBUG
            
            if token and all(c.isalnum() or c in "._-" for c in token):
                access_token = token
            else:
                from starlette.responses import JSONResponse
                return JSONResponse(
                    status_code=400,
                    content={"error": "Invalid authorization token format"}
                )
        
        success = await auth.logout(
            refresh_token=refresh_token,
            access_token=access_token,
        )
        return {"success": success}
    
    except Exception as e:
        logger.error(f"Logout error: {type(e).__name__}: {str(e)}")
        from starlette.responses import JSONResponse
        return JSONResponse(
            status_code=500,
            content={"error": f"Internal server error: {type(e).__name__}"}
        )

@router.get("/me", response_model=UserResponse, responses={401: {"model": ErrorResponse}})
async def me(request: Request):
    """Get current authenticated user info.

    Joins the ``users`` DB row on top of the in-memory auth user
    so the response carries avatar_url, created_at, last_seen_at,
    and phone - the fields the Flutter Settings > General screen
    hydrates from in one round-trip.
    """
    user = request.state.user
    if user is None:
        # Dev mode (auth disabled) - middleware sets user_id="local",
        # roles=["admin"], permissions=["*"]. Return a synthetic admin
        # stub so Flutter sees is_admin=true locally.
        state_perms = list(getattr(request.state, "permissions", []) or [])
        state_roles = list(getattr(request.state, "roles", []) or [])
        state_uid = getattr(request.state, "user_id", "anonymous")
        if state_uid and state_uid != "anonymous":
            return UserResponse(
                user_id=state_uid,
                email=None,
                display_name=state_uid.title(),
                roles=state_roles,
                permissions=state_perms,
                is_admin=_is_admin(state_roles, state_perms),
            )
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    avatar_url: str | None = None
    created_at: str | None = None
    last_seen_at: str | None = None
    phone: str | None = None

    try:
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import User
        from sqlalchemy import select as _sel
        async with get_session_factory()() as db:
            row = (
                await db.execute(
                    _sel(User).where(User.id == user.user_id)
                )
            ).scalar_one_or_none()
            if row is not None:
                avatar_url = row.avatar_url
                created_at = row.created_at.isoformat() if row.created_at else None
                last_seen_at = (
                    row.last_seen_at.isoformat() if row.last_seen_at else None
                )
                phone = row.phone
    except Exception:
        # Best-effort join - if the users table isn't populated
        # (anonymous / dev mode), we still return the basic fields.
        pass

    return UserResponse(
        user_id=user.user_id,
        email=user.email,
        display_name=user.display_name,
        roles=user.roles or [],
        permissions=user.permissions or [],
        is_admin=_is_admin(user.roles, user.permissions),
        avatar_url=avatar_url,
        created_at=created_at,
        last_seen_at=last_seen_at,
        phone=phone,
    )


# ─── OAuth (Google, Microsoft) ───────────────────────────────────────
#
# Two endpoints implement the standard authorization-code flow:
#
#   GET /auth/oauth/{provider}            -> 302 to provider's consent screen
#   GET /auth/oauth/{provider}/callback   -> exchange code, mint JWT, bounce
#                                            to the web app with the token
#                                            in the URL fragment (#token=...)
#
# `provider` is the registered ID (`google`, `microsoft`). Auto-provisioning
# is enabled in OAuth2Provider so the first sign-in of a new email creates
# the user row on the fly.

_OAUTH_PROVIDERS = {"google", "microsoft"}

# Per-state bounce target overrides for desktop OAuth. The Flutter desktop
# app starts a tiny localhost HTTP server, calls /auth/oauth/{provider}
# with `?bounce_to=http://127.0.0.1:<port>/oauth-callback`, and we deliver
# the JWT to that URL instead of the default web_origin. Keyed by the
# OAuth state nonce so the callback can look it up after the round-trip.
_BOUNCE_OVERRIDES: dict[str, str] = {}


def _is_safe_localhost(url: str) -> bool:
    """Only allow http://127.0.0.1:<port>/* or http://localhost:<port>/*
    as a `bounce_to` target. Prevents this endpoint from being abused as
    an open redirect to arbitrary external URLs.
    """
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme != "http":
        return False
    if u.hostname not in ("127.0.0.1", "localhost", "::1"):
        return False
    return True


def _web_origin_for_callback(request: Request) -> str:
    """Resolve the front-end origin to bounce back to after OAuth.

    Defaults to the configured `oauth.web_origin`; if unset, falls back to
    the request's `Origin` / `Referer` (handy for dev where the daemon and
    web run on different ports).
    """
    settings = getattr(request.app.state, "settings", None)
    cfg = getattr(settings, "oauth", None) if settings else None
    web = getattr(cfg, "web_origin", None) if cfg else None
    if web:
        return web.rstrip("/")
    referer = request.headers.get("referer") or request.headers.get("origin")
    if referer:
        return referer.split("?", 1)[0].rstrip("/")
    return "http://localhost:3000"


@router.get("/oauth/{provider}")
async def oauth_start(provider: str, request: Request):
    """Step 1: redirect the browser to the OAuth provider's consent screen.

    Optional `bounce_to` query param: a localhost URL the daemon should
    redirect to after auth (instead of the default web_origin). Used by
    the Flutter desktop app, which spins up a temporary local HTTP server
    to receive the JWT.
    """
    if provider not in _OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider '{provider}'")
    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:
        raise HTTPException(status_code=503, detail="Auth service not initialized")
    prov = auth_service.get_provider(provider)
    if prov is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"OAuth provider '{provider}' is not configured. Set "
                f"DIGITORN_OAUTH__{provider.upper()}__CLIENT_ID and "
                f"DIGITORN_OAUTH__{provider.upper()}__CLIENT_SECRET."
            ),
        )
    url, state = prov.get_authorize_url()
    bounce_to = request.query_params.get("bounce_to", "").strip()
    if bounce_to and _is_safe_localhost(bounce_to):
        # Cap the dict so a malicious caller can't fill memory; clean
        # entries are popped by the callback, abandoned ones evict on age.
        if len(_BOUNCE_OVERRIDES) > 1000:
            _BOUNCE_OVERRIDES.clear()
        _BOUNCE_OVERRIDES[state] = bounce_to
    return RedirectResponse(url, status_code=302)


@router.get("/oauth/{provider}/callback")
async def oauth_callback(provider: str, request: Request):
    """Step 2: provider redirected back here with `code` + `state`. Exchange
    them for a JWT, then bounce the browser to the web app with the token.
    """
    if provider not in _OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider '{provider}'")
    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:
        raise HTTPException(status_code=503, detail="Auth service not initialized")

    web = _web_origin_for_callback(request)
    state = request.query_params.get("state", "")
    # Pop the desktop bounce override (if any) so a single state is one-shot.
    desktop_bounce = _BOUNCE_OVERRIDES.pop(state, None)

    error = request.query_params.get("error")
    if error:
        # User clicked Deny on the consent screen, or provider rejected
        # the request (e.g. bad redirect_uri). Bounce with the error.
        params = urlencode({"oauth_error": error})
        if desktop_bounce:
            return RedirectResponse(f"{desktop_bounce}?{params}", status_code=302)
        return RedirectResponse(f"{web}/login?{params}", status_code=302)

    code = request.query_params.get("code", "")
    if not code:
        params = urlencode({"oauth_error": "missing_code"})
        if desktop_bounce:
            return RedirectResponse(f"{desktop_bounce}?{params}", status_code=302)
        return RedirectResponse(f"{web}/login?{params}", status_code=302)

    result = await auth_service.login(
        {"code": code, "state": state}, provider=provider,
    )
    if not result.success or not result.access_token:
        params = urlencode({"oauth_error": result.error or "auth_failed"})
        if desktop_bounce:
            return RedirectResponse(f"{desktop_bounce}?{params}", status_code=302)
        return RedirectResponse(f"{web}/login?{params}", status_code=302)

    # Desktop callers (Flutter local HTTP server) need the token in the
    # query string because http.server cannot read URL fragments. The
    # localhost-only allowlist in oauth_start protects against leakage.
    if desktop_bounce:
        params = urlencode({
            "access_token": result.access_token,
            "refresh_token": result.refresh_token or "",
            "expires_in": str(result.expires_in or 0),
            "provider": provider,
        })
        return RedirectResponse(f"{desktop_bounce}?{params}", status_code=302)

    # Token in the URL fragment (#) instead of the query (?) so it never
    # hits server logs or referer headers when the SPA redirects further.
    fragment = urlencode({
        "access_token": result.access_token,
        "refresh_token": result.refresh_token or "",
        "expires_in": str(result.expires_in or 0),
        "provider": provider,
    })
    return RedirectResponse(f"{web}/auth/oauth-return#{fragment}", status_code=302)


# NOTE: cross-app session routes (``GET /auth/sessions``,
# ``GET /auth/sessions/{sid}/history``, ``POST /auth/sessions/{sid}/fork``,
# ``DELETE /auth/sessions/{sid}``) were removed on 2026-04-21. They were
# redundant with the app-scoped equivalents under ``/api/apps/{app_id}/...``
# which are what the client actually uses. Sessions belong to an app,
# not to ``/auth`` - the UI lists them when the user opens an app.
#
# Canonical replacements:
#   GET  /api/apps/{app_id}/sessions                          (list)
#   GET  /api/apps/{app_id}/sessions/{session_id}/history     (messages)
#   DELETE /api/apps/{app_id}/sessions/{session_id}           (delete)
#   POST /api/apps/{app_id}/sessions/{session_id}/fork        (fork)


# Avant de créer le router, ajouter un middleware de debug
async def debug_headers_middleware(request: Request, call_next):
    """Middleware pour logger les headers bruts avant validation."""
    auth_header = request.headers.get("authorization", "")
    if auth_header:
        logger.info(f"Raw Authorization header: {repr(auth_header)}")
        logger.info(f"Header type: {type(auth_header)}")
        logger.info(f"Header bytes: {auth_header.encode('utf-8', errors='replace')}")
    
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        logger.error(f"Middleware error: {type(e).__name__}: {str(e)}", exc_info=True)
        raise
