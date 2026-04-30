"""Login / register / refresh / logout endpoints.

The actual logic lives in ``digitorn_auth.service.AuthService``; this
router is a thin HTTP wrapper around it. Mirrors the daemon's existing
``/auth/*`` paths so clients don't have to change anything when we
flip the endpoint base URL from the daemon to this service.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from digitorn_auth.api.deps import (
    bearer_scheme,
    get_auth_service,
    require_user,
)
from digitorn_auth.service import AuthService
from digitorn_auth.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Request / response models ──────────────────────────────────────


class LoginRequest(BaseModel):
    username: str | None = None
    email: EmailStr | None = None
    password: str = Field(..., min_length=1)
    provider: str | None = None


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8)
    email: EmailStr | None = None
    display_name: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    email: str | None = None
    display_name: str | None = None
    roles: list[str] = []
    permissions: list[str] = []


# ── Endpoints ──────────────────────────────────────────────────────


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
):
    """Verify credentials and return access + refresh tokens."""
    creds = body.model_dump(exclude_none=True)
    creds.pop("provider", None)
    # The local provider accepts EITHER `username` OR `email` and looks
    # the user up on whichever was supplied (see providers/local.py).
    # Stringify the EmailStr so SQLAlchemy gets a plain str.
    if "email" in creds:
        creds["email"] = str(creds["email"])

    device_info = request.headers.get("user-agent", "")[:512] or None
    result = await auth.login(creds, provider=body.provider, device_info=device_info)
    if not result.success:
        raise HTTPException(status_code=401, detail=result.error or "Login failed")

    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        user_id=result.user_id,
        email=result.email,
        display_name=result.display_name,
        roles=result.roles or [],
        permissions=result.permissions or [],
    )


@router.post("/register", response_model=TokenResponse)
async def register(
    body: RegisterRequest,
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
):
    """Create a new local-provider account and return tokens."""
    device_info = request.headers.get("user-agent", "")[:512] or None
    result = await auth.register(
        username=body.username,
        password=body.password,
        email=str(body.email) if body.email else None,
        display_name=body.display_name,
        device_info=device_info,
    )
    if not result.success:
        raise HTTPException(status_code=400, detail=result.error or "Registration failed")

    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        user_id=result.user_id,
        email=result.email,
        display_name=result.display_name,
        roles=result.roles or [],
        permissions=result.permissions or [],
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    auth: Annotated[AuthService, Depends(get_auth_service)],
):
    """Exchange a refresh token for a fresh access token (and a rolled refresh)."""
    result = await auth.refresh(body.refresh_token)
    if not result.success:
        raise HTTPException(status_code=401, detail=result.error or "Refresh failed")

    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        user_id=result.user_id,
        email=result.email,
        display_name=result.display_name,
        roles=result.roles or [],
        permissions=result.permissions or [],
    )


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    user: Annotated[User, Depends(require_user)],
    body: RefreshRequest | None = None,
):
    """Revoke the current access token (always) and refresh token (if
    provided in the body). The access token's jti goes into the
    durable revocation list so daemons polling
    ``GET /auth/admin/revocations`` pick it up — no waiting for the
    natural exp.
    """
    auth_header = request.headers.get("authorization", "")
    access_token = (
        auth_header.split(" ", 1)[1].strip()
        if auth_header.lower().startswith("bearer ")
        else None
    )
    refresh_token = body.refresh_token if body else None
    await auth.logout(refresh_token=refresh_token, access_token=access_token)


# ── User-owned sessions (refresh tokens) ────────────────────────────


class SessionItem(BaseModel):
    id: str
    device_info: str | None = None
    created_at: str
    expires_at: str


@router.get("/sessions", response_model=list[SessionItem])
async def list_user_sessions(
    user: Annotated[User, Depends(require_user)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
):
    """List the calling user's active browser/device sessions.

    Each row is one un-revoked, un-expired refresh token. ``device_info``
    is the User-Agent captured at login. The user can revoke a specific
    session via ``DELETE /auth/sessions/{id}``.
    """
    rows = await auth.list_sessions(user.id)
    return rows


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_user_session(
    session_id: str,
    user: Annotated[User, Depends(require_user)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
):
    """Revoke ONE of the calling user's sessions (refresh tokens).

    Ownership is enforced server-side — passing another user's id
    returns 404 (we don't reveal whether the row exists).
    """
    revoked = await auth.revoke_session(user.id, session_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("/me")
async def me(
    user: Annotated[User, Depends(require_user)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
):
    """Return the current user's profile (sanity check + dashboard fetch).

    Includes the resolved feature flags so a UI client can render the
    right surface (cloud paywall, premium-only apps, device cap, …)
    without parsing the JWT itself.
    """
    features = await auth._get_account_features(user.id)
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "provider": user.provider,
        "external_id": user.external_id,
        "avatar_url": user.avatar_url,
        "created_at": user.created_at.isoformat(),
        "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
        "features": features or {
            # Default for users without an explicit AccountFeatures row.
            "plan_tier": "free",
            "cloud_enabled": False,
            "self_host_enabled": True,
            "cloud_token_quota_monthly": 0,
            "max_paired_devices": 5,
            "extra": {},
        },
    }
