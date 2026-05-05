"""HTTP routes for quota inspection (user-facing) and configuration
(admin-facing). All routes here REPLACE the equivalent endpoints
that lived on the daemon - the daemon will redirect / deprecate
them as part of the cleanup phase.

Authorization model:

* `/v1/quota/*`        - any authenticated user, returns their own data only.
* `/admin/quota/*`     - requires `admin` role on the JWT (`roles: ["admin"]`).
                        The check is strict: missing role -> 403.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_gateway.auth import GatewayPrincipal, require_principal
from digitorn_gateway.db import session_dependency
from digitorn_gateway.models_db import Plan, QuotaCounter, UserPlan
from digitorn_gateway.plans import get_registry
from digitorn_gateway.quota import get_engine
from digitorn_gateway.quota_schema import (
    PlanCreateRequest,
    PlanUpdateRequest,
    UserPlanAssignRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Helpers ────────────────────────────────────────────────────────


def _require_admin(principal: GatewayPrincipal) -> None:
    if "admin" not in principal.roles:
        raise HTTPException(403, detail="admin_role_required")


def _plan_to_dict(p: Plan) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "is_default": p.is_default,
        "quota_def": p.quota_def or {},
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


# ── User-facing ────────────────────────────────────────────────────


@router.get("/v1/quota/me")
async def get_my_quota(
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Return the caller's current usage + the limits they're on."""
    registry = get_registry()
    engine = get_engine()

    quota_def = await registry.resolve(principal.user_id)
    snapshot = engine.snapshot(principal.user_id)
    return {
        "user_id": principal.user_id,
        "limits": (
            quota_def.model_dump(exclude_none=True) if quota_def else None
        ),
        "usage": snapshot,
    }


# ── Admin: plans ───────────────────────────────────────────────────


@router.get("/admin/quota/plans")
async def admin_list_plans(
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    rows = (await db.execute(select(Plan))).scalars().all()
    return {"data": [_plan_to_dict(p) for p in rows]}


@router.post("/admin/quota/plans")
async def admin_create_plan(
    body: PlanCreateRequest,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    existing = (
        await db.execute(select(Plan).where(Plan.id == body.id))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, detail=f"plan_exists: {body.id}")
    if body.is_default:
        # Demote previous defaults so exactly one is the default at any time.
        defaults = (
            await db.execute(select(Plan).where(Plan.is_default.is_(True)))
        ).scalars().all()
        for d in defaults:
            d.is_default = False
    row = Plan(
        id=body.id,
        name=body.name,
        description=body.description,
        is_default=body.is_default,
        quota_def=body.quota_def.model_dump(exclude_none=True),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    # In-memory caches are stale after a write; reload.
    await get_registry().reload_plans()
    return _plan_to_dict(row)


@router.get("/admin/quota/plans/{plan_id}")
async def admin_get_plan(
    plan_id: str,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = (
        await db.execute(select(Plan).where(Plan.id == plan_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail=f"plan_not_found: {plan_id}")
    return _plan_to_dict(row)


@router.put("/admin/quota/plans/{plan_id}")
async def admin_update_plan(
    plan_id: str,
    body: PlanUpdateRequest,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = (
        await db.execute(select(Plan).where(Plan.id == plan_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail=f"plan_not_found: {plan_id}")
    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    if body.is_default is not None:
        if body.is_default and not row.is_default:
            others = (
                await db.execute(select(Plan).where(Plan.is_default.is_(True)))
            ).scalars().all()
            for d in others:
                if d.id != plan_id:
                    d.is_default = False
        row.is_default = body.is_default
    if body.quota_def is not None:
        row.quota_def = body.quota_def.model_dump(exclude_none=True)
    await db.commit()
    await db.refresh(row)
    await get_registry().reload_plans()
    return _plan_to_dict(row)


@router.delete("/admin/quota/plans/{plan_id}")
async def admin_delete_plan(
    plan_id: str,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = (
        await db.execute(select(Plan).where(Plan.id == plan_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail=f"plan_not_found: {plan_id}")
    if row.is_default:
        raise HTTPException(
            409,
            detail="cannot_delete_default_plan: assign another plan as default first",
        )
    # Refuse to delete a plan that still has users on it - deletion
    # would orphan them.
    using = (
        await db.execute(
            select(UserPlan).where(UserPlan.plan_id == plan_id).limit(1),
        )
    ).scalar_one_or_none()
    if using is not None:
        raise HTTPException(
            409,
            detail=f"plan_in_use: at least one user is on plan {plan_id!r}",
        )
    await db.delete(row)
    await db.commit()
    await get_registry().reload_plans()
    return {"deleted": True, "plan_id": plan_id}


# ── Admin: user assignments ───────────────────────────────────────


@router.get("/admin/quota/users/{user_id}")
async def admin_get_user_quota(
    user_id: str,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    registry = get_registry()
    engine = get_engine()
    row = (
        await db.execute(
            select(UserPlan).where(UserPlan.user_id == user_id),
        )
    ).scalar_one_or_none()
    quota_def = await registry.resolve(user_id)
    snapshot = engine.snapshot(user_id)
    return {
        "user_id": user_id,
        "plan_id": row.plan_id if row else None,
        "has_override": bool(row and row.override_quota_def),
        "override_quota_def": (row.override_quota_def if row else None),
        "effective_limits": (
            quota_def.model_dump(exclude_none=True) if quota_def else None
        ),
        "usage": snapshot,
    }


@router.put("/admin/quota/users/{user_id}")
async def admin_assign_user_plan(
    user_id: str,
    body: UserPlanAssignRequest,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    plan_row = (
        await db.execute(select(Plan).where(Plan.id == body.plan_id))
    ).scalar_one_or_none()
    if plan_row is None:
        raise HTTPException(404, detail=f"plan_not_found: {body.plan_id}")

    row = (
        await db.execute(select(UserPlan).where(UserPlan.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        row = UserPlan(
            user_id=user_id,
            plan_id=body.plan_id,
            override_quota_def=(
                body.override_quota_def.model_dump(exclude_none=True)
                if body.override_quota_def else None
            ),
        )
        db.add(row)
    else:
        row.plan_id = body.plan_id
        row.override_quota_def = (
            body.override_quota_def.model_dump(exclude_none=True)
            if body.override_quota_def else None
        )
    await db.commit()
    get_registry().invalidate_user(user_id)
    return {
        "user_id": user_id,
        "plan_id": body.plan_id,
        "has_override": bool(body.override_quota_def),
    }


@router.delete("/admin/quota/users/{user_id}")
async def admin_reset_user_quota(
    user_id: str,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Wipe a user's counters + sticky block. Does NOT remove their
    plan assignment - use a follow-up DELETE on the user_plans row
    or PUT to a different plan if you want to change tier.
    """
    _require_admin(principal)
    await get_engine().reset_user(user_id)
    return {"user_id": user_id, "reset": True}
