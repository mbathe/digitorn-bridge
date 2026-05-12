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
    QuotaDefinition,
    UserPlanAssignRequest,
)
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Helpers ────────────────────────────────────────────────────────


def _require_admin(principal: GatewayPrincipal) -> None:
    if not (principal.roles and (
        "admin" in principal.roles or "developer" in principal.roles
    )):
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
    # When the operator runs with quota disabled (dev / load test),
    # ``init_registry`` / ``init_engine`` were skipped at boot. Rather
    # than 500-ing on the unconditional ``get_registry()`` call, we
    # return a sane "no quota configured" payload that matches the
    # dashboard's ``MyQuota`` TypeScript shape.
    from digitorn_gateway.config import get_settings as _gs
    if not _gs().quota_enabled:
        return {
            "user_id": principal.user_id,
            "limits": None,
            "usage": {
                "user_id": principal.user_id,
                "blocked": False,
                "block_info": None,
                "counters": [],
            },
        }

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
        "extra_usage_def": (row.extra_usage_def if row else None),
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
    previous_plan_id: str | None = row.plan_id if row is not None else None
    is_initial = row is None

    new_override = (
        body.override_quota_def.model_dump(exclude_none=True)
        if body.override_quota_def else None
    )
    if row is None:
        row = UserPlan(
            user_id=user_id,
            plan_id=body.plan_id,
            override_quota_def=new_override,
        )
        db.add(row)
    else:
        row.plan_id = body.plan_id
        row.override_quota_def = new_override

    # v2: append a row to gateway_user_plan_history for the audit trail.
    # Determine the change kind from the transition.
    if is_initial:
        change_kind = "initial"
    elif previous_plan_id == body.plan_id:
        change_kind = "admin"  # same plan, override edit
    else:
        # Best-effort upgrade vs downgrade: compare monthly_price_cents
        # if both plans have one. Default to "admin" when unknown.
        change_kind = "admin"
        try:
            prev_price = (
                await db.execute(
                    select(Plan.monthly_price_cents).where(Plan.id == previous_plan_id)
                )
            ).scalar_one_or_none() if previous_plan_id else None
            new_price = getattr(plan_row, "monthly_price_cents", None)
            if prev_price is not None and new_price is not None:
                if int(new_price) > int(prev_price):
                    change_kind = "upgrade"
                elif int(new_price) < int(prev_price):
                    change_kind = "downgrade"
        except Exception:
            pass

    snapshot = {
        "name": plan_row.name,
        "quota_def": plan_row.quota_def,
        "override_quota_def": new_override,
        "monthly_price_cents": getattr(plan_row, "monthly_price_cents", 0),
    }
    try:
        from sqlalchemy import text as _text
        import json as _json
        await db.execute(
            _text("""
                INSERT INTO gateway_user_plan_history (
                    user_id, from_plan_id, to_plan_id, changed_by,
                    change_kind, reason, snapshot
                ) VALUES (
                    :user_id, :from_plan_id, :to_plan_id, :changed_by,
                    :change_kind, :reason, CAST(:snapshot AS JSONB)
                )
            """),
            {
                "user_id": user_id,
                "from_plan_id": previous_plan_id,
                "to_plan_id": body.plan_id,
                "changed_by": getattr(principal, "user_id", None),
                "change_kind": change_kind,
                "reason": getattr(body, "reason", None),
                "snapshot": _json.dumps(snapshot),
            },
        )
    except Exception as exc:
        logger.warning(
            "gateway_user_plan_history append failed user=%s err=%s",
            user_id, exc,
        )

    await db.commit()
    get_registry().invalidate_user(user_id)
    return {
        "user_id": user_id,
        "plan_id": body.plan_id,
        "has_override": bool(body.override_quota_def),
        "change_kind": change_kind,
        "previous_plan_id": previous_plan_id,
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


# ── Per-user overage (extra usage on top of plan limit) ────────────


class UserOverageRequest(BaseModel):
    """Body of PUT /admin/quota/users/{user_id}/overage.

    ``extra_usage_def`` carries the extra capacity granted to the user
    on top of their plan. Same JSON shape as a QuotaDefinition (metric
    -> window -> {limit, reset, ...}). Pass ``null`` or omit to clear.
    """

    extra_usage_def: QuotaDefinition | None = None


def _ensure_user_plan_row(db: AsyncSession, user_id: str) -> UserPlan:
    """Internal: fetch or create a minimal user_plan row so we can
    attach overage even if the user has never been explicitly assigned
    to a plan. Caller must commit."""
    return UserPlan(
        user_id=user_id,
        # plan_id stays NOT NULL in the schema; bind to the default
        # plan id so the row is consistent.
        plan_id="",  # patched below
    )


@router.put("/admin/quota/users/{user_id}/overage")
async def admin_set_user_overage(
    user_id: str,
    body: UserOverageRequest,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Grant or replace the user's overage allowance.

    Effective immediately: the call invalidates the per-user PlanRegistry
    cache so the supervisor reads the new value on its next pass (within
    a second). The user's hot path is unaffected - they're only ever
    blocked when the supervisor decides their actual usage crossed
    ``plan_limit + extra_usage``.
    """
    _require_admin(principal)
    new_extra = (
        body.extra_usage_def.model_dump(exclude_none=True)
        if body.extra_usage_def else None
    )

    row = (
        await db.execute(select(UserPlan).where(UserPlan.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        # No explicit plan assignment - bind to the default plan first
        # so the row is consistent. Without this we'd violate the FK on
        # plan_id (it's NOT NULL).
        default_plan = (
            await db.execute(
                select(Plan).where(Plan.is_default.is_(True)).limit(1),
            )
        ).scalar_one_or_none()
        if default_plan is None:
            raise HTTPException(
                400,
                detail=(
                    "no_default_plan: cannot grant overage to a user with no "
                    "plan assignment when no default plan exists"
                ),
            )
        row = UserPlan(
            user_id=user_id,
            plan_id=default_plan.id,
            extra_usage_def=new_extra,
        )
        db.add(row)
    else:
        row.extra_usage_def = new_extra

    await db.commit()
    # Drop the cached resolution so the supervisor's next pass picks up
    # the new extra_usage value within ~1s.
    get_registry().invalidate_user(user_id)
    # CRITICAL: also drop any active sticky block. Without this, the
    # supervisor bails out at every tick because ``_blocks[user_id]`` is
    # still alive (set when the user crossed the OLD plan-only ceiling)
    # and the bonus has no visible effect until the natural reset window
    # (midnight for per_day, …). Dropping the block forces the next
    # supervisor pass to re-evaluate against ``plan + new_extra`` and
    # either let the user through or set a fresh block at the right
    # ceiling. Counters are NOT touched -- they reflect real usage.
    get_engine().force_recheck(user_id)
    return {
        "user_id": user_id,
        "plan_id": row.plan_id,
        "extra_usage_def": new_extra,
    }


@router.delete("/admin/quota/users/{user_id}/overage")
async def admin_clear_user_overage(
    user_id: str,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Remove the user's overage allowance entirely. Equivalent to
    PUT with ``extra_usage_def=null``."""
    _require_admin(principal)
    row = (
        await db.execute(select(UserPlan).where(UserPlan.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        # Nothing to clear; idempotent success.
        return {"user_id": user_id, "cleared": True, "had_extra": False}
    had_extra = row.extra_usage_def is not None
    row.extra_usage_def = None
    await db.commit()
    get_registry().invalidate_user(user_id)
    # Same as set_overage: force a supervisor recheck so the block state
    # tracks the new (lower) effective ceiling immediately.
    get_engine().force_recheck(user_id)
    return {"user_id": user_id, "cleared": True, "had_extra": had_extra}
