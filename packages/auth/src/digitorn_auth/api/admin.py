"""Admin endpoints for AccountFeatures management.

Locked to ``admin``-role users. The dashboard / billing webhook hits
this to set someone's plan / quotas after a purchase. Daemons never
touch these endpoints.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_auth.api.deps import get_auth_service, get_db, require_user
from digitorn_auth.models import AccountFeatures, User
from digitorn_auth.service import AuthService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/admin", tags=["admin"])


# ── Models ─────────────────────────────────────────────────────────


class AccountFeaturesPayload(BaseModel):
    plan_tier: str = Field(default="free", description="free | pro | enterprise")
    cloud_enabled: bool = False
    self_host_enabled: bool = True
    cloud_token_quota_monthly: int = Field(default=0, ge=0)
    max_paired_devices: int = Field(default=5, ge=0)
    flags: dict[str, Any] = Field(default_factory=dict)


class AccountFeaturesResponse(AccountFeaturesPayload):
    user_id: str


# ── Helpers ────────────────────────────────────────────────────────


async def _require_admin(
    user: Annotated[User, Depends(require_user)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> User:
    """Reject unless the calling user has the 'admin' role."""
    roles = await auth._get_user_roles(user.id)
    if "admin" not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user


def _row_to_response(row: AccountFeatures) -> AccountFeaturesResponse:
    return AccountFeaturesResponse(
        user_id=row.user_id,
        plan_tier=row.plan_tier,
        cloud_enabled=row.cloud_enabled,
        self_host_enabled=row.self_host_enabled,
        cloud_token_quota_monthly=row.cloud_token_quota_monthly,
        max_paired_devices=row.max_paired_devices,
        flags=row.flags or {},
    )


# ── Endpoints ──────────────────────────────────────────────────────


@router.get(
    "/account-features/{user_id}",
    response_model=AccountFeaturesResponse,
)
async def get_features(
    user_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(_require_admin)],
):
    """Get the AccountFeatures row for a user.

    Returns 404 if no row exists yet (the user is on the implicit
    free plan).
    """
    row = await db.get(AccountFeatures, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No features row")
    return _row_to_response(row)


@router.put(
    "/account-features/{user_id}",
    response_model=AccountFeaturesResponse,
)
async def upsert_features(
    user_id: str,
    payload: AccountFeaturesPayload,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(_require_admin)],
):
    """Create or update the AccountFeatures row for a user.

    The new values take effect on the user's NEXT login or refresh
    (the JWT carries a snapshot, so existing tokens stay on the old
    values until they expire — usually 15 min).
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    row = await db.get(AccountFeatures, user_id)
    if row is None:
        row = AccountFeatures(user_id=user_id)
        db.add(row)
    row.plan_tier = payload.plan_tier
    row.cloud_enabled = payload.cloud_enabled
    row.self_host_enabled = payload.self_host_enabled
    row.cloud_token_quota_monthly = payload.cloud_token_quota_monthly
    row.max_paired_devices = payload.max_paired_devices
    row.flags = payload.flags or {}
    await db.commit()
    await db.refresh(row)
    logger.info(
        "account_features_updated user_id=%s plan=%s cloud=%s",
        user_id, row.plan_tier, row.cloud_enabled,
    )
    return _row_to_response(row)


@router.delete(
    "/account-features/{user_id}",
    status_code=204,
)
async def delete_features(
    user_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(_require_admin)],
):
    """Drop the AccountFeatures row, reverting the user to default."""
    row = await db.get(AccountFeatures, user_id)
    if row is None:
        return
    await db.delete(row)
    await db.commit()


# ── Token revocation ───────────────────────────────────────────────


class RevokeRequest(BaseModel):
    jti: str = Field(..., description="JWT ID (sub-claim) of the token to kill.")
    user_id: str = Field(..., description="Owner of the token.")
    expires_at: int = Field(
        ..., description="Original token's exp (epoch s). Caps the deny-list lifetime.",
    )
    reason: str = Field(default="admin_revoke")


class RevocationItem(BaseModel):
    jti: str
    user_id: str
    expires_at: str
    reason: str
    revoked_at: str


@router.post("/revoke", status_code=204)
async def revoke_token(
    body: RevokeRequest,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    _admin: Annotated[User, Depends(_require_admin)],
):
    """Add a jti to the deny-list. Effective immediately on this
    process; other replicas pick it up via the next ``GET /revocations``
    sync.
    """
    await auth.revoke_jti(
        jti=body.jti,
        user_id=body.user_id,
        expires_at=float(body.expires_at),
        reason=body.reason,
    )


@router.get("/revocations", response_model=list[RevocationItem])
async def list_revocations(
    auth: Annotated[AuthService, Depends(get_auth_service)],
    _admin: Annotated[User, Depends(_require_admin)],
    since: float = 0,
    only_active: bool = True,
):
    """List active revocations (admin only).

    Daemons (or any other replica) call this periodically, e.g. every
    30s, with ``since=<last_revoked_at>`` to incrementally pick up
    new revocations without paging the whole table.
    """
    rows = await auth.list_revocations(since=since, only_active=only_active)
    return rows


# ── Public revocations feed (consumed by daemons) ──────────────────


public_revocations_router = APIRouter(prefix="/auth", tags=["revocations"])


class PublicRevocationItem(BaseModel):
    jti: str
    expires_at: int = Field(..., description="Epoch seconds; 0 = never expires")
    revoked_at: int = Field(..., description="Epoch seconds when revoked")


@public_revocations_router.get(
    "/revocations",
    response_model=list[PublicRevocationItem],
)
async def public_list_revocations(
    auth: Annotated[AuthService, Depends(get_auth_service)],
    since: float = 0,
):
    """Public revocation feed for offline-verifying clients.

    Returns the same data as /auth/admin/revocations but stripped of
    user_id/reason and without admin auth so any consumer (daemon,
    sidecar, edge worker) can poll it. Knowing a jti is revoked
    grants no access on its own - the list is just opaque IDs.
    """
    rows = await auth.list_revocations(since=since, only_active=True)
    out: list[PublicRevocationItem] = []
    from datetime import datetime
    for row in rows:
        try:
            exp_iso = row.get("expires_at") if isinstance(row, dict) else getattr(row, "expires_at", None)
            rev_iso = row.get("revoked_at") if isinstance(row, dict) else getattr(row, "revoked_at", None)
            jti = row.get("jti") if isinstance(row, dict) else getattr(row, "jti", None)
        except Exception:
            continue
        if not jti:
            continue
        out.append(PublicRevocationItem(
            jti=jti,
            expires_at=int(datetime.fromisoformat(exp_iso).timestamp()) if isinstance(exp_iso, str) else int(exp_iso or 0),
            revoked_at=int(datetime.fromisoformat(rev_iso).timestamp()) if isinstance(rev_iso, str) else int(rev_iso or 0),
        ))
    return out


@router.post("/revoke-all/{user_id}", status_code=200)
async def revoke_all_for_user(
    user_id: str,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    _admin: Annotated[User, Depends(_require_admin)],
):
    """Mass-revoke every refresh token for a user (log out everywhere).

    Returns the number of refresh tokens flipped from active → revoked.
    Access tokens already in the wild stay valid until their own exp
    (default 15 min), but no new tokens can be minted via the revoked
    refreshes.
    """
    count = await auth.revoke_all_for_user(user_id)
    return {"refresh_tokens_revoked": count}


# ── Admin user CRUD (replaces the daemon's ex-/api/admin/users) ────


class AdminUserView(BaseModel):
    """User row exposed via the admin API. Mirrors the daemon's old
    serializer so the existing dashboard table renders unchanged."""

    id: str
    external_id: str
    provider: str
    email: str | None
    display_name: str | None
    phone: str | None
    avatar_url: str | None
    is_active: bool
    created_at: str | None
    updated_at: str | None
    last_seen_at: str | None
    attributes: dict[str, Any]
    roles: list[str]


class AdminUserListResponse(BaseModel):
    users: list[AdminUserView]
    total: int
    limit: int
    offset: int
    has_more: bool


class AdminUserUpdateRequest(BaseModel):
    """PATCH body. All fields optional; only set ones get applied."""

    display_name: str | None = None
    phone: str | None = None
    is_active: bool | None = None
    attributes: dict[str, Any] | None = None


_ADMIN_RESERVED_ATTRS = frozenset({
    "password_hash", "password_set_at", "lockout_until",
    "lockout_count", "last_failure_at",
    "mfa_secret", "mfa_recovery_codes", "mfa_enabled",
})


def _safe_admin_attrs(attrs: dict[str, Any] | None) -> dict[str, Any]:
    if not attrs:
        return {}
    return {k: v for k, v in attrs.items() if k not in _ADMIN_RESERVED_ATTRS}


async def _serialize_user(
    auth: AuthService, row: User,
) -> AdminUserView:
    roles = await auth._get_user_roles(row.id)
    return AdminUserView(
        id=row.id,
        external_id=row.external_id,
        provider=row.provider,
        email=row.email,
        display_name=row.display_name,
        phone=row.phone,
        avatar_url=row.avatar_url,
        is_active=row.is_active,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        last_seen_at=row.last_seen_at.isoformat() if row.last_seen_at else None,
        attributes=_safe_admin_attrs(row.attributes),
        roles=roles,
    )


@router.get("/users", response_model=AdminUserListResponse)
async def admin_list_users(
    auth: Annotated[AuthService, Depends(get_auth_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(_require_admin)],
    q: str = "",
    is_active: str = "",
    provider: str = "",
    limit: int = 50,
    offset: int = 0,
) -> AdminUserListResponse:
    """Paginated user listing for the admin panel.

    Filters compose: ``q`` is a substring match on email / display_name
    / external_id, ``is_active`` is "true"/"false"/"" (any),
    ``provider`` matches exactly. Returns the same shape the daemon
    used to so the existing dashboard table works without changes.
    """
    from sqlalchemy import select, func, or_, and_
    stmt = select(User)
    conds = []
    if q:
        needle = f"%{q.strip().lower()}%"
        conds.append(or_(
            User.email.ilike(needle),
            User.display_name.ilike(needle),
            User.external_id.ilike(needle),
        ))
    if is_active.lower() in ("true", "1", "yes"):
        conds.append(User.is_active.is_(True))
    elif is_active.lower() in ("false", "0", "no"):
        conds.append(User.is_active.is_(False))
    if provider:
        conds.append(User.provider == provider)
    if conds:
        stmt = stmt.where(and_(*conds))

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0

    stmt = stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()

    users = [await _serialize_user(auth, u) for u in rows]
    return AdminUserListResponse(
        users=users,
        total=int(total),
        limit=limit,
        offset=offset,
        has_more=(offset + len(rows)) < int(total),
    )


@router.get("/users/{user_id}", response_model=AdminUserView)
async def admin_get_user(
    user_id: str,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(_require_admin)],
):
    """Return one user by id."""
    row = await db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return await _serialize_user(auth, row)


@router.patch("/users/{user_id}", response_model=AdminUserView)
async def admin_update_user(
    user_id: str,
    body: AdminUserUpdateRequest,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(_require_admin)],
):
    """Update an arbitrary user's profile (admin-only).

    Same field set as the self-service PATCH /auth/me, plus
    ``is_active`` to soft-disable an account. Reserved attribute keys
    are filtered.
    """
    row = await db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    if body.display_name is not None:
        row.display_name = body.display_name.strip() or None
    if body.phone is not None:
        row.phone = body.phone.strip() or None
    if body.is_active is not None:
        row.is_active = body.is_active
    if body.attributes is not None:
        merged = dict(row.attributes or {})
        merged.update(_safe_admin_attrs(body.attributes))
        row.attributes = merged
    await db.commit()
    await db.refresh(row)
    return await _serialize_user(auth, row)


@router.delete("/users/{user_id}", status_code=200)
async def admin_delete_user(
    user_id: str,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    admin_user: Annotated[User, Depends(_require_admin)],
    hard: bool = False,
):
    """Soft-delete (default) or hard-delete a user.

    Soft delete flips ``is_active=false`` and revokes all refresh
    tokens (logout everywhere). Hard delete cascades the row and
    every related FK.
    """
    if user_id == admin_user.id:
        raise HTTPException(
            status_code=400, detail="You cannot delete your own account.",
        )
    row = await db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    if hard:
        await db.delete(row)
        await db.commit()
        return {"user_id": user_id, "deleted": True, "kind": "hard"}
    row.is_active = False
    await db.commit()
    revoked = await auth.revoke_all_for_user(user_id)
    return {
        "user_id": user_id,
        "deleted": True,
        "kind": "soft",
        "refresh_tokens_revoked": revoked,
    }
