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
    """List active revocations.

    Daemons (or any other replica) call this periodically, e.g. every
    30s, with ``since=<last_revoked_at>`` to incrementally pick up
    new revocations without paging the whole table.
    """
    rows = await auth.list_revocations(since=since, only_active=only_active)
    return rows


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
