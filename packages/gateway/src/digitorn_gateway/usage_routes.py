"""Read-only aggregations for the admin usage dashboard.

Powers the recharts widgets on the dashboard's `Usage` page:

* timeline of cost / tokens / requests by day (configurable window)
* top users by cost
* top models by cost
* current month totals (single-row summary)

All routes return JSON ready for direct consumption by recharts -
no further client-side reshaping. The aggregations run against the
`gateway_quota_counters` table populated by the quota engine.

Performance: every endpoint is a single SQL aggregation. No
in-memory snapshot of the engine is consulted - the engine is
designed for hot-path serving, not for analytics. Stale data of
up to `flush_interval_seconds` is acceptable here.

Authorization: admin role required (same gate as `/admin/quota/*`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_gateway.auth import GatewayPrincipal, require_principal
from digitorn_gateway.db import session_dependency
from digitorn_gateway.models_db import QuotaCounter

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_admin(principal: GatewayPrincipal) -> None:
    if "admin" not in principal.roles:
        raise HTTPException(403, detail="admin_role_required")


# ── Summary ────────────────────────────────────────────────────────


@router.get("/admin/usage/summary")
async def admin_usage_summary(
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Single-row totals across the whole month-to-date window."""
    _require_admin(principal)
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    metrics = ("requests", "tokens_total", "cost_usd")
    out: dict[str, float] = {m: 0.0 for m in metrics}
    for metric in metrics:
        stmt = select(func.coalesce(func.sum(QuotaCounter.value), 0.0)).where(
            QuotaCounter.metric == metric,
            QuotaCounter.reset_at >= month_start,
        )
        out[metric] = float((await db.execute(stmt)).scalar() or 0.0)

    # Active users = distinct users with any counter row in the window.
    active_users_stmt = (
        select(func.count(func.distinct(QuotaCounter.user_id)))
        .where(QuotaCounter.reset_at >= month_start)
    )
    active_users = int((await db.execute(active_users_stmt)).scalar() or 0)

    return {
        "window": {
            "from": month_start.isoformat(),
            "to": now.isoformat(),
            "label": "month_to_date",
        },
        "active_users": active_users,
        "totals": out,
    }


# ── Timeline (per-day cost / tokens / requests) ────────────────────


@router.get("/admin/usage/timeline")
async def admin_usage_timeline(
    days: int = 30,
    metric: str = "cost_usd",
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Return a per-day series for the requested metric. The result is
    a flat list ready for `recharts` (LineChart / AreaChart / BarChart)::

        {
          "metric": "cost_usd",
          "days": 30,
          "series": [
            {"date": "2026-04-05", "value": 0.0},
            {"date": "2026-04-06", "value": 0.012},
            ...
          ]
        }

    Days with no data return value=0 so the chart has continuous
    coverage.
    """
    _require_admin(principal)

    if metric not in ("requests", "messages", "tokens_input", "tokens_output", "tokens_total", "cost_usd"):
        raise HTTPException(400, detail=f"unknown_metric: {metric!r}")
    if days < 1 or days > 365:
        raise HTTPException(400, detail="days must be between 1 and 365")

    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days - 1)

    # Counters carry their bucket via window_key. For the per-day view
    # we only count daily-aligned buckets - per_day / per_week /
    # per_month would otherwise inflate the daily total. Filter by the
    # `window_key` segment.
    rows = (
        await db.execute(
            select(QuotaCounter)
            .where(
                QuotaCounter.metric == metric,
                QuotaCounter.reset_at >= start,
                QuotaCounter.reset_at < now + timedelta(days=1),
                QuotaCounter.window_key.like(f"{metric}|per_day|%"),
            )
        )
    ).scalars().all()

    by_day: dict[str, float] = {}
    for r in rows:
        # Bucket end = day end. Bucket start = day end - 1 day.
        bucket_start = r.reset_at - timedelta(days=1)
        date_key = bucket_start.date().isoformat()
        by_day[date_key] = by_day.get(date_key, 0.0) + float(r.value)

    # Fill gaps with zeros for chart continuity.
    series: list[dict[str, Any]] = []
    for i in range(days):
        d = (start + timedelta(days=i)).date()
        series.append({"date": d.isoformat(), "value": round(by_day.get(d.isoformat(), 0.0), 6)})

    return {
        "metric": metric,
        "days": days,
        "series": series,
    }


# ── Top users / models ─────────────────────────────────────────────


@router.get("/admin/usage/top-users")
async def admin_usage_top_users(
    days: int = 30,
    metric: str = "cost_usd",
    limit: int = 20,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Top N users by accumulated metric over the window.

    Useful for spotting outliers: who is consuming the most tokens
    or costing the most this month.
    """
    _require_admin(principal)
    if metric not in ("requests", "messages", "tokens_total", "tokens_input", "tokens_output", "cost_usd"):
        raise HTTPException(400, detail=f"unknown_metric: {metric!r}")
    if days < 1 or days > 365:
        raise HTTPException(400, detail="days must be between 1 and 365")
    if limit < 1 or limit > 100:
        raise HTTPException(400, detail="limit must be between 1 and 100")

    start = datetime.now(timezone.utc) - timedelta(days=days)

    stmt = (
        select(
            QuotaCounter.user_id,
            func.sum(QuotaCounter.value).label("total"),
        )
        .where(
            QuotaCounter.metric == metric,
            QuotaCounter.reset_at >= start,
            # Same per-day filter as timeline - prevents double-counting
            # the same activity across multiple windows (per_day,
            # per_week, per_month all contain the same events).
            QuotaCounter.window_key.like(f"{metric}|per_day|%"),
        )
        .group_by(QuotaCounter.user_id)
        .order_by(func.sum(QuotaCounter.value).desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()

    return {
        "metric": metric,
        "days": days,
        "users": [
            {"user_id": r.user_id, "value": round(float(r.total), 6)}
            for r in rows
        ],
    }
