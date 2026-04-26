"""Auth API endpoints — login, register, token refresh, user management.

All endpoints return JSON. Token-based authentication via Bearer header.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, FastAPI
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
    """Logout body — everything is optional.

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
    # Extra UI metadata — avatar_url / created_at / last_seen_at
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
    and phone — the fields the Flutter Settings > General screen
    hydrates from in one round-trip.
    """
    user = request.state.user
    if user is None:
        # Dev mode (auth disabled) — middleware sets user_id="local",
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
        # Best-effort join — if the users table isn't populated
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

# NOTE: cross-app session routes (``GET /auth/sessions``,
# ``GET /auth/sessions/{sid}/history``, ``POST /auth/sessions/{sid}/fork``,
# ``DELETE /auth/sessions/{sid}``) were removed on 2026-04-21. They were
# redundant with the app-scoped equivalents under ``/api/apps/{app_id}/...``
# which are what the client actually uses. Sessions belong to an app,
# not to ``/auth`` — the UI lists them when the user opens an app.
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
