"""Login / register / refresh / logout endpoints.

The actual logic lives in ``digitorn_auth.service.AuthService``; this
router is a thin HTTP wrapper around it. Mirrors the daemon's existing
``/auth/*`` paths so clients don't have to change anything when we
flip the endpoint base URL from the daemon to this service.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_auth.api.deps import (
    bearer_scheme,
    get_auth_service,
    get_db,
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
    """Return the current user's full profile.

    This is the canonical identity endpoint - every consuming service
    (daemons, dashboards) reads identity from here, never from a local
    table. Includes resolved feature flags so a UI client can render
    the right surface (cloud paywall, premium-only apps, device cap)
    without parsing the JWT itself.
    """
    features = await auth._get_account_features(user.id)
    roles = await auth._get_user_roles(user.id)
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "phone": user.phone,
        "provider": user.provider,
        "external_id": user.external_id,
        "avatar_url": user.avatar_url,
        "attributes": user.attributes or {},
        "is_active": user.is_active,
        "roles": roles,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
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


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = None
    phone: str | None = None
    attributes: dict[str, Any] | None = None


# Reserved keys clients are not allowed to overwrite via PATCH.
# Anything in here is daemon-managed: lockout counters, password
# hashes, MFA state, internal flags. Drop on input, never echo back.
_RESERVED_ATTRS = frozenset({
    "password_hash", "password_set_at", "lockout_until",
    "lockout_count", "last_failure_at",
    "mfa_secret", "mfa_recovery_codes", "mfa_enabled",
})


def _safe_attributes(attrs: dict[str, Any] | None) -> dict[str, Any]:
    if not attrs:
        return {}
    return {k: v for k, v in attrs.items() if k not in _RESERVED_ATTRS}


@router.patch("/me")
async def update_me(
    body: ProfileUpdateRequest,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update the caller's mutable profile fields.

    Only ``display_name``, ``phone``, and ``attributes`` are writable
    here. Email is locked (re-verification flow lives elsewhere).
    Avatar is uploaded via POST /auth/me/avatar. Attributes get
    merged shallow, with reserved (daemon-managed) keys filtered.
    """
    changed: dict[str, Any] = {}
    if body.display_name is not None:
        user.display_name = body.display_name.strip() or None
        changed["display_name"] = user.display_name
    if body.phone is not None:
        user.phone = body.phone.strip() or None
        changed["phone"] = user.phone
    if body.attributes is not None:
        merged = dict(user.attributes or {})
        # Preserve reserved keys (filter input) so a malicious client
        # can't lift its own lockout by sending {lockout_count: 0}.
        merged.update(_safe_attributes(body.attributes))
        user.attributes = merged
        changed["attributes"] = _safe_attributes(merged)
    if changed:
        await db.commit()
        await db.refresh(user)
    return {
        "id": user.id,
        "display_name": user.display_name,
        "phone": user.phone,
        "attributes": _safe_attributes(user.attributes),
        "avatar_url": user.avatar_url,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        "changed": list(changed.keys()),
    }


class PasswordChangeRequest(BaseModel):
    current: str = Field(..., min_length=1, description="Current password")
    new: str = Field(..., min_length=8, description="New password (8+ chars)")


@router.post("/me/password", status_code=204)
async def change_my_password(
    body: PasswordChangeRequest,
    user: Annotated[User, Depends(require_user)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
):
    """Change the caller's password.

    Only works for users authenticated against the local provider
    (email + password). OAuth-managed accounts (Google, Microsoft)
    don't have a local password to change - this returns 400.

    The current password is verified first; on success the new hash
    is stored and a 204 is returned. On failure, 400 with a generic
    "Current password is incorrect" message (no enumeration of which
    field was wrong).
    """
    try:
        ok = await auth.change_password(
            user_id=user.id, current=body.current, new=body.new,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AttributeError:
        raise HTTPException(
            status_code=400,
            detail="Password change not supported for this account type",
        )
    if not ok:
        raise HTTPException(status_code=400, detail="Current password is incorrect")


# ── Avatar upload + serve ──────────────────────────────────────────


_ALLOWED_AVATAR_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}


@router.post("/me/avatar")
async def upload_my_avatar(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Upload an avatar image. Multipart, field name ``file`` or
    ``avatar``. Stored on the auth-service persistent volume; the URL
    returned is served by ``GET /auth/avatars/{filename}``.
    """
    from pathlib import Path
    from datetime import datetime, timezone
    from digitorn_auth.config import get_settings

    form = await request.form()
    file = form.get("file") or form.get("avatar")
    if file is None or not hasattr(file, "filename"):
        raise HTTPException(status_code=400, detail="No file provided")

    fn = getattr(file, "filename", "") or ""
    ext = "png"
    if "." in fn:
        ext = fn.rsplit(".", 1)[-1].lower()
    if ext not in _ALLOWED_AVATAR_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image format: {ext!r}. Allowed: {sorted(_ALLOWED_AVATAR_EXTS)}",
        )

    settings = get_settings()
    data = await file.read()
    if len(data) > settings.avatar_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Avatar exceeds {settings.avatar_max_bytes} bytes",
        )

    avatar_dir = Path(settings.avatar_dir)
    avatar_dir.mkdir(parents=True, exist_ok=True)
    safe_uid = user.id.replace("/", "_").replace("\\", "_")
    target = avatar_dir / f"{safe_uid}.{ext}"
    target.write_bytes(data)

    # URL points at the public-served avatar route. Path is opaque
    # (no host) so the client builds it relative to whichever origin
    # is reaching the auth service.
    avatar_url = f"/auth/avatars/{target.name}"
    user.avatar_url = avatar_url
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return {"avatar_url": avatar_url, "bytes": len(data)}


@router.delete("/me/avatar", status_code=204)
async def delete_my_avatar(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Drop the caller's avatar. Idempotent."""
    from pathlib import Path
    from datetime import datetime, timezone
    from digitorn_auth.config import get_settings

    if user.avatar_url:
        # avatar_url is /auth/avatars/<filename> - extract filename
        filename = user.avatar_url.rsplit("/", 1)[-1]
        target = Path(get_settings().avatar_dir) / filename
        try:
            if target.is_file():
                target.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.debug("avatar_unlink_failed file=%s exc=%s", target, exc)
    user.avatar_url = None
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()


# Public router (no auth) - serves avatars by filename. Filenames are
# user-public by design (they show up in chat headers, member lists,
# etc.) and the filename embeds the user_id so enumeration is moot.
public_avatar_router = APIRouter(prefix="/auth", tags=["avatars"])


@public_avatar_router.get("/avatars/{filename}")
async def serve_avatar(filename: str):
    """Serve a stored avatar file. No auth required."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    from digitorn_auth.config import get_settings

    safe = Path(filename).name  # strip path components
    target = Path(get_settings().avatar_dir) / safe
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(str(target))
