"""Read-only dashboard endpoints powered by the v2 SQL views.

Four routes, each backed by one Postgres view created in migrations:

    GET /admin/agents/running        → v_agents_running
    GET /admin/agents/top-cost-7d    → v_agents_top_cost_7d
    GET /admin/users/{uid}/quota     → v_user_quota_state (single row)
    GET /admin/usage/top-users-month → v_usage_top_users_month

Why views, not ad-hoc queries: the views encapsulate the joins
(session_agents → agent_runs → users → gateway_quota_counters), and
swapping their definition is a non-breaking change for the frontend.

Authorization: admin role required.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_gateway.auth import GatewayPrincipal, require_principal
from digitorn_gateway.db import session_dependency

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_admin(principal: GatewayPrincipal) -> None:
    if "admin" not in principal.roles:
        raise HTTPException(403, detail="admin_role_required")


# ── /admin/agents/running ─────────────────────────────────────────


@router.get("/admin/agents/running")
async def admin_agents_running(
    limit: int = 100,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Every agent run currently ``queued`` or ``active``.

    The view ``v_agents_running`` joins agent_runs to session_agents
    and exposes elapsed_ms + event_count so the dashboard can render
    a "what's running now" panel without further client-side joins.
    """
    _require_admin(principal)
    if limit < 1 or limit > 500:
        raise HTTPException(400, detail="limit must be between 1 and 500")

    rows = (await db.execute(
        text("""
            SELECT run_id, session_agent_id, specialist_key, specialist_name,
                   session_pk, user_id, specialist, provider, model, status,
                   queued_at, started_at, last_event_at,
                   elapsed_ms, turns_used, max_turns,
                   total_tokens, total_cost_usd, event_count
            FROM v_agents_running
            ORDER BY started_at DESC NULLS LAST
            LIMIT :lim
        """),
        {"lim": limit},
    )).mappings().all()

    return {
        "count": len(rows),
        "rows": [_serialize_row(r) for r in rows],
    }


# ── /admin/agents/top-cost-7d ─────────────────────────────────────


@router.get("/admin/agents/top-cost-7d")
async def admin_agents_top_cost_7d(
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Top users by completed-run cost over the rolling 7 days."""
    _require_admin(principal)
    rows = (await db.execute(
        text("""
            SELECT user_id, run_count, tokens_7d, cost_7d_usd,
                   turns_7d, avg_duration_ms
            FROM v_agents_top_cost_7d
        """)
    )).mappings().all()
    return {
        "count": len(rows),
        "rows": [_serialize_row(r) for r in rows],
    }


# ── /admin/users/{user_id}/quota ──────────────────────────────────


@router.get("/admin/users/{user_id}/quota-state")
async def admin_user_quota_state(
    user_id: str,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Effective quota_def + counter snapshot + active block for a user."""
    _require_admin(principal)
    row = (await db.execute(
        text("""
            SELECT user_id, plan_id, plan_name, effective_quota_def,
                   blocked_until, block_reason, block_metric, counters
            FROM v_user_quota_state
            WHERE user_id = :user_id
            LIMIT 1
        """),
        {"user_id": user_id},
    )).mappings().first()
    if row is None:
        raise HTTPException(404, detail="user_not_found")
    return _serialize_row(row)


# ── /admin/usage/top-users-month ──────────────────────────────────


@router.get("/admin/usage/top-users-month")
async def admin_usage_top_users_month(
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Top users by gateway_usage_events spend in the current month."""
    _require_admin(principal)
    rows = (await db.execute(
        text("""
            SELECT user_id, event_count, tokens_month,
                   cost_month_usd, avg_latency_ms, last_event_at
            FROM v_usage_top_users_month
        """)
    )).mappings().all()
    return {
        "count": len(rows),
        "rows": [_serialize_row(r) for r in rows],
    }


# ── Helpers ────────────────────────────────────────────────────────


def _serialize_row(row: Any) -> dict[str, Any]:
    """JSON-serialise a row mapping. Datetimes -> ISO strings, Decimal
    -> float. Everything else passes through verbatim.
    """
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif hasattr(v, "__float__") and not isinstance(v, (bool, int, float)):
            # Decimal-like - cast to float for JSON.
            out[k] = float(v)
        else:
            out[k] = v
    return out
