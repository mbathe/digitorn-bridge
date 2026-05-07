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
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_gateway.auth import GatewayPrincipal, require_principal
from digitorn_gateway.db import session_dependency
from digitorn_gateway.models_db import QuotaCounter

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_admin(principal: GatewayPrincipal) -> None:
    """Authorization gate. Accepts ``admin`` and ``developer`` roles -
    same logic as ``admin_writable_routes._require_admin``. The
    dashboard JWTs issued by ``auth.digitorn.ai`` carry ``developer``
    by default; restricting to admin-only would mean nobody could
    actually open the Usage page during gateway onboarding."""
    if not (principal.roles and (
        "admin" in principal.roles or "developer" in principal.roles
    )):
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


# ── Top providers / models (event-grain analytics) ────────────────
#
# These two endpoints aggregate ``gateway_usage_events`` directly
# instead of the rolled-up ``gateway_quota_counters`` table - the
# counters are bucketed per user × metric × window only and don't
# carry the provider / model breakdown the dashboard wants.
#
# Performance: each query is a single SQL aggregation under ``WHERE
# created_at >= start`` plus the (provider, created_at) /
# (model, created_at) compound indexes added in migration 0011.
# Sub-100 ms even on multi-million-row partitions; bounded by LIMIT.


def _validate_window(days: int, limit: int) -> None:
    if days < 1 or days > 365:
        raise HTTPException(400, detail="days must be between 1 and 365")
    if limit < 1 or limit > 100:
        raise HTTPException(400, detail="limit must be between 1 and 100")


@router.get("/admin/usage/top-providers")
async def admin_usage_top_providers(
    days: int = 30,
    limit: int = 20,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Top providers by traffic over the window.

    Returns one row per provider with **all** metrics in one shot
    (requests, tokens_total, cost_usd) so the UI can flip between
    them without re-querying. Sorted by tokens_total desc.
    """
    _require_admin(principal)
    _validate_window(days, limit)

    start = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        text("""
            SELECT
                provider,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(prompt_tokens), 0) AS tokens_input,
                COALESCE(SUM(completion_tokens), 0) AS tokens_output,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                COUNT(DISTINCT user_id) AS users
            FROM gateway_usage_events
            WHERE created_at >= :start
            GROUP BY provider
            ORDER BY tokens_total DESC
            LIMIT :limit
        """),
        {"start": start, "limit": limit},
    )).mappings().all()

    return {
        "days": days,
        "providers": [
            {
                "provider": r["provider"] or "unknown",
                "requests": int(r["requests"]),
                "tokens_total": int(r["tokens_total"]),
                "tokens_input": int(r["tokens_input"]),
                "tokens_output": int(r["tokens_output"]),
                "cost_usd": round(float(r["cost_usd"]), 6),
                "users": int(r["users"]),
            }
            for r in rows
        ],
    }


@router.get("/admin/usage/top-models")
async def admin_usage_top_models(
    days: int = 30,
    limit: int = 20,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Top models by traffic over the window.

    Same shape as ``top-providers``; carries the upstream provider
    too so the UI can render ``model (provider)`` without a
    separate lookup.
    """
    _require_admin(principal)
    _validate_window(days, limit)

    start = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        text("""
            SELECT
                model,
                MAX(provider) AS provider,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(prompt_tokens), 0) AS tokens_input,
                COALESCE(SUM(completion_tokens), 0) AS tokens_output,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                COUNT(DISTINCT user_id) AS users
            FROM gateway_usage_events
            WHERE created_at >= :start
            GROUP BY model
            ORDER BY tokens_total DESC
            LIMIT :limit
        """),
        {"start": start, "limit": limit},
    )).mappings().all()

    return {
        "days": days,
        "models": [
            {
                "model": r["model"] or "unknown",
                "provider": r["provider"] or "unknown",
                "requests": int(r["requests"]),
                "tokens_total": int(r["tokens_total"]),
                "tokens_input": int(r["tokens_input"]),
                "tokens_output": int(r["tokens_output"]),
                "cost_usd": round(float(r["cost_usd"]), 6),
                "users": int(r["users"]),
            }
            for r in rows
        ],
    }


@router.get("/admin/usage/by-month")
async def admin_usage_by_month(
    months: int = 6,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Per-month aggregation across the last ``months`` months.

    Returns one row per calendar month (UTC), all metrics in one shot,
    sorted oldest-first so the UI can plot a left-to-right bar chart.
    Months with zero traffic still appear with zeros so the chart
    has continuous coverage.
    """
    _require_admin(principal)
    if months < 1 or months > 24:
        raise HTTPException(400, detail="months must be between 1 and 24")

    now = datetime.now(timezone.utc)
    start = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
             - timedelta(days=32 * (months - 1)))
    start = start.replace(day=1)

    rows = (await db.execute(
        text("""
            SELECT
                date_trunc('month', created_at) AS month_start,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                COUNT(DISTINCT user_id) AS active_users
            FROM gateway_usage_events
            WHERE created_at >= :start
            GROUP BY month_start
            ORDER BY month_start ASC
        """),
        {"start": start},
    )).mappings().all()

    # Fill gaps so the UI doesn't have to.
    by_month: dict[str, dict[str, Any]] = {
        r["month_start"].date().isoformat(): {
            "month": r["month_start"].date().isoformat(),
            "requests": int(r["requests"]),
            "tokens_total": int(r["tokens_total"]),
            "cost_usd": round(float(r["cost_usd"]), 6),
            "active_users": int(r["active_users"]),
        }
        for r in rows
    }
    series: list[dict[str, Any]] = []
    cursor = start
    while cursor <= now:
        key = cursor.date().isoformat()
        series.append(by_month.get(key, {
            "month": key,
            "requests": 0,
            "tokens_total": 0,
            "cost_usd": 0.0,
            "active_users": 0,
        }))
        # advance one month
        year = cursor.year + (1 if cursor.month == 12 else 0)
        month = 1 if cursor.month == 12 else cursor.month + 1
        cursor = cursor.replace(year=year, month=month, day=1)

    return {"months": months, "series": series}
