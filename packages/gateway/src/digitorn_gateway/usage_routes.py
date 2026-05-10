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
    # Source of truth = ``gateway_usage_events`` (the per-call audit
    # log), grouped by UTC date. ``QuotaCounter`` is the live aggregate
    # used for blocking decisions but its buckets reset (and rows are
    # deleted) at window end, so it can't serve a 30-day historical
    # chart. The events table is append-only with monthly partitions.
    metric_to_col = {
        "requests": "count(*)",
        "messages": "count(*)",
        "tokens_input": "coalesce(sum(prompt_tokens), 0)",
        "tokens_output": "coalesce(sum(completion_tokens), 0)",
        "tokens_total": "coalesce(sum(total_tokens), 0)",
        "cost_usd": "coalesce(sum(total_cost_usd), 0)",
    }
    select_expr = metric_to_col[metric]
    from sqlalchemy import text as _text
    rows = (
        await db.execute(
            _text(f"""
                select
                    date_trunc('day', created_at at time zone 'UTC') as day,
                    {select_expr} as value
                from gateway_usage_events
                where created_at >= :start
                  and created_at < :end
                group by 1
                order by 1
            """),
            {"start": start, "end": now + timedelta(days=1)},
        )
    ).fetchall()

    by_day: dict[str, float] = {}
    for r in rows:
        date_key = r[0].date().isoformat()
        by_day[date_key] = float(r[1])

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


# ── Latency percentiles per provider ──────────────────────────────
#
# Uses the existing ``(provider, created_at)`` index for the range
# filter, then sorts the latency_ms values per group. With ~100k
# rows / 5 providers the in-memory sort is sub-100ms. Latency = NULL
# rows (errored before measurement) are excluded.


@router.get("/admin/usage/latency-stats")
async def admin_usage_latency_stats(
    days: int = 7,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """p50 / p95 / p99 latency_ms per provider over the window.

    Useful for SLA monitoring and provider comparison. Returns one
    row per provider sorted by request count desc.
    """
    _require_admin(principal)
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be between 1 and 90")

    start = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        text("""
            SELECT
                provider,
                COUNT(*) AS samples,
                COALESCE(percentile_cont(0.5)
                    WITHIN GROUP (ORDER BY latency_ms), 0) AS p50,
                COALESCE(percentile_cont(0.95)
                    WITHIN GROUP (ORDER BY latency_ms), 0) AS p95,
                COALESCE(percentile_cont(0.99)
                    WITHIN GROUP (ORDER BY latency_ms), 0) AS p99,
                COALESCE(MAX(latency_ms), 0) AS max_ms
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND latency_ms IS NOT NULL
            GROUP BY provider
            ORDER BY samples DESC
            LIMIT 50
        """),
        {"start": start},
    )).mappings().all()

    return {
        "days": days,
        "providers": [
            {
                "provider": r["provider"] or "unknown",
                "samples": int(r["samples"]),
                "p50_ms": int(r["p50"]),
                "p95_ms": int(r["p95"]),
                "p99_ms": int(r["p99"]),
                "max_ms": int(r["max_ms"]),
            }
            for r in rows
        ],
    }


# ── Error breakdown per provider ──────────────────────────────────
#
# Same index path as latency. Errors are typically <10% of total
# events so the post-filter (``error_class IS NOT NULL``) is cheap.


@router.get("/admin/usage/error-breakdown")
async def admin_usage_error_breakdown(
    days: int = 7,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Error counts per (provider, error_class) over the window.

    Returns one row per error type with the affected provider and
    a count. Includes a global total for context (so the UI can
    render an error rate as a percentage of all calls).
    """
    _require_admin(principal)
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be between 1 and 90")

    start = datetime.now(timezone.utc) - timedelta(days=days)

    total_stmt = text("""
        SELECT COUNT(*) AS total
        FROM gateway_usage_events
        WHERE created_at >= :start
    """)
    total = int((await db.execute(total_stmt, {"start": start})).scalar() or 0)

    rows = (await db.execute(
        text("""
            SELECT provider, error_class, COUNT(*) AS errors
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND error_class IS NOT NULL
              AND error_class <> ''
            GROUP BY provider, error_class
            ORDER BY errors DESC
            LIMIT 100
        """),
        {"start": start},
    )).mappings().all()

    return {
        "days": days,
        "total_requests": total,
        "errors": [
            {
                "provider": r["provider"] or "unknown",
                "error_class": r["error_class"],
                "count": int(r["errors"]),
            }
            for r in rows
        ],
    }


# ── Stacked timeline by provider OR model ─────────────────────────
#
# Pivots the per-day series so the UI can render a stacked area /
# bar chart with one series per provider (or model). Pivoting is
# done client-side - we return a flat list of
# ``{date, dimension_key, value}`` rows that recharts can transform
# via groupBy.


@router.get("/admin/usage/timeline-stacked")
async def admin_usage_timeline_stacked(
    dimension: str = "provider",
    metric: str = "tokens_total",
    days: int = 30,
    top: int = 6,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Per-day series broken down by provider OR model.

    ``dimension`` = ``provider`` | ``model``. Top N busiest
    dimensions are returned in full, the rest grouped under
    ``other`` so the chart stays legible.
    """
    _require_admin(principal)
    if dimension not in ("provider", "model"):
        raise HTTPException(400, detail="dimension must be 'provider' or 'model'")
    if metric not in ("requests", "tokens_total", "cost_usd"):
        raise HTTPException(400, detail=f"unknown_metric: {metric!r}")
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be between 1 and 90")
    if top < 1 or top > 20:
        raise HTTPException(400, detail="top must be between 1 and 20")

    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ) - timedelta(days=days - 1)

    metric_sql = {
        "requests": "COUNT(*)",
        "tokens_total": "COALESCE(SUM(prompt_tokens + completion_tokens), 0)",
        "cost_usd": "COALESCE(SUM(total_cost_usd), 0)",
    }[metric]

    # First identify the top N dimensions over the window.
    top_stmt = text(f"""
        SELECT {dimension} AS dim, {metric_sql} AS total
        FROM gateway_usage_events
        WHERE created_at >= :start
        GROUP BY {dimension}
        ORDER BY total DESC
        LIMIT :top
    """)
    top_dims = [
        r["dim"] or "unknown"
        for r in (await db.execute(
            top_stmt, {"start": start, "top": top},
        )).mappings().all()
    ]

    # Then fetch the daily series for those top dims (and roll up
    # everything else into ``other``).
    series_stmt = text(f"""
        SELECT
            date_trunc('day', created_at) AS day,
            {dimension} AS dim,
            {metric_sql} AS value
        FROM gateway_usage_events
        WHERE created_at >= :start
        GROUP BY day, {dimension}
        ORDER BY day ASC
    """)
    raw = (await db.execute(series_stmt, {"start": start})).mappings().all()

    top_set = set(top_dims)
    series: list[dict[str, Any]] = []
    for r in raw:
        dim = r["dim"] or "unknown"
        bucket = dim if dim in top_set else "other"
        series.append({
            "date": r["day"].date().isoformat(),
            "dimension": bucket,
            "value": float(r["value"]) if metric == "cost_usd" else int(r["value"]),
        })

    return {
        "dimension": dimension,
        "metric": metric,
        "days": days,
        "top_dimensions": top_dims,
        "series": series,
    }


# ── Hourly heatmap (24h × 7-day-of-week) ──────────────────────────
#
# One ``(hour, day_of_week)`` cell with a count(*). Useful to spot
# usage patterns: when do users hit hardest? Range-filter via
# ``created_at >= start`` uses the standard partition + index path,
# then EXTRACT is computed in-memory per row (cheap).


@router.get("/admin/usage/hourly-heatmap")
async def admin_usage_hourly_heatmap(
    days: int = 30,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Request count per (hour, day_of_week) cell, UTC."""
    _require_admin(principal)
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be between 1 and 90")

    start = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        text("""
            SELECT
                EXTRACT(HOUR FROM created_at)::int AS hour,
                EXTRACT(ISODOW FROM created_at)::int AS dow,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
            FROM gateway_usage_events
            WHERE created_at >= :start
            GROUP BY hour, dow
            ORDER BY dow, hour
        """),
        {"start": start},
    )).mappings().all()

    # Fill empty cells so the UI has 168 cells total (7 × 24).
    by_cell: dict[tuple[int, int], dict[str, int]] = {}
    for r in rows:
        by_cell[(int(r["dow"]), int(r["hour"]))] = {
            "requests": int(r["requests"]),
            "tokens": int(r["tokens"]),
        }

    cells: list[dict[str, Any]] = []
    for dow in range(1, 8):  # ISO: 1=Mon, 7=Sun
        for hour in range(24):
            data = by_cell.get((dow, hour), {"requests": 0, "tokens": 0})
            cells.append({
                "dow": dow,
                "hour": hour,
                "requests": data["requests"],
                "tokens": data["tokens"],
            })

    return {"days": days, "cells": cells}


# ── Observability stats (cache, failover, served-by, truncation) ─────
#
# Aggregates over the 5 columns added by migration 0014. Powers the
# new widgets on the dashboard's Usage page so the operator can see
# at a glance how often each runtime safety net actually fires.
#
# Performance: same single-aggregation pattern as the other admin
# endpoints. The ``ix_gateway_usage_events_cache_hit`` partial index
# keeps the cache panel cheap even on large partitions.


@router.get("/admin/usage/cache-stats")
async def admin_usage_cache_stats(
    days: int = 7,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Cache hit/miss totals + a daily timeline.

    The hit rate is the headline metric: it answers "how often did
    the response cache save a real LLM call?". Daily timeline lets
    the operator see if the rate is climbing (good - users are
    re-asking the same question) or falling (bad - cache invalidation
    hint, or the prompts have started varying).
    """
    _require_admin(principal)
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be between 1 and 90")

    start = datetime.now(timezone.utc) - timedelta(days=days)

    totals = (await db.execute(
        text("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE cache_hit) AS hits
            FROM gateway_usage_events
            WHERE created_at >= :start
        """),
        {"start": start},
    )).mappings().first()
    total = int(totals["total"] or 0)
    hits = int(totals["hits"] or 0)
    rate = (hits / total) if total > 0 else 0.0

    timeline_rows = (await db.execute(
        text("""
            SELECT
                date_trunc('day', created_at) AS day,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE cache_hit) AS hits
            FROM gateway_usage_events
            WHERE created_at >= :start
            GROUP BY day
            ORDER BY day
        """),
        {"start": start},
    )).mappings().all()

    return {
        "days": days,
        "total_requests": total,
        "cache_hits": hits,
        "hit_rate": round(rate, 4),
        "timeline": [
            {
                "day": r["day"].date().isoformat(),
                "total": int(r["total"] or 0),
                "hits": int(r["hits"] or 0),
                "hit_rate": (
                    round(int(r["hits"] or 0) / int(r["total"] or 1), 4)
                ),
            }
            for r in timeline_rows
        ],
    }


@router.get("/admin/usage/failover-stats")
async def admin_usage_failover_stats(
    days: int = 7,
    limit: int = 10,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """How often the failover loop walked beyond the primary route.

    Returns:
      * total_requests / failover_requests / failover_rate
      * top providers most frequently FAILING (= appearing as the
        first slug in the trail without being the served_by). Indicates
        which credentials are degrading.
      * top served_by under failover (which providers most often saved
        the day).
    """
    _require_admin(principal)
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be between 1 and 90")
    if limit < 1 or limit > 50:
        raise HTTPException(400, detail="limit must be between 1 and 50")

    start = datetime.now(timezone.utc) - timedelta(days=days)

    totals = (await db.execute(
        text("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE attempts > 1) AS failovered
            FROM gateway_usage_events
            WHERE created_at >= :start
        """),
        {"start": start},
    )).mappings().first()
    total = int(totals["total"] or 0)
    failovered = int(totals["failovered"] or 0)
    rate = (failovered / total) if total > 0 else 0.0

    # Failing-providers ranking: providers that appeared in the trail
    # but were NOT the final served_by. failover_trail is JSONB list
    # of slugs; jsonb_array_elements_text expands them.
    failing = (await db.execute(
        text("""
            SELECT failing_provider, COUNT(*) AS hits
            FROM (
                SELECT
                    served_by,
                    jsonb_array_elements_text(failover_trail) AS failing_provider
                FROM gateway_usage_events
                WHERE created_at >= :start
                  AND attempts > 1
                  AND failover_trail IS NOT NULL
            ) sub
            WHERE failing_provider IS DISTINCT FROM served_by
            GROUP BY failing_provider
            ORDER BY hits DESC
            LIMIT :limit
        """),
        {"start": start, "limit": limit},
    )).mappings().all()

    # Saved-by ranking: served_by under failover (the survivor).
    saved_by = (await db.execute(
        text("""
            SELECT served_by, COUNT(*) AS hits
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND attempts > 1
              AND served_by IS NOT NULL
            GROUP BY served_by
            ORDER BY hits DESC
            LIMIT :limit
        """),
        {"start": start, "limit": limit},
    )).mappings().all()

    return {
        "days": days,
        "total_requests": total,
        "failover_requests": failovered,
        "failover_rate": round(rate, 4),
        "failing_providers": [
            {"provider": r["failing_provider"], "events": int(r["hits"])}
            for r in failing
        ],
        "saved_by": [
            {"provider": r["served_by"], "events": int(r["hits"])}
            for r in saved_by
        ],
    }


@router.get("/admin/usage/served-by")
async def admin_usage_served_by(
    days: int = 7,
    limit: int = 20,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Breakdown by served_by, the canonical answer to 'who actually
    served my requests'. Differs from /top-providers when failover
    fires - the user requested provider X, but Y ended up answering."""
    _require_admin(principal)
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be between 1 and 90")
    if limit < 1 or limit > 50:
        raise HTTPException(400, detail="limit must be between 1 and 50")

    start = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        text("""
            SELECT
                served_by,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                AVG(latency_ms) AS avg_latency_ms,
                COUNT(*) FILTER (WHERE attempts > 1) AS via_failover
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND served_by IS NOT NULL
            GROUP BY served_by
            ORDER BY requests DESC
            LIMIT :limit
        """),
        {"start": start, "limit": limit},
    )).mappings().all()

    return {
        "days": days,
        "served_by": [
            {
                "provider": r["served_by"],
                "requests": int(r["requests"]),
                "tokens_total": int(r["tokens_total"]),
                "cost_usd": round(float(r["cost_usd"]), 6),
                "avg_latency_ms": (
                    int(r["avg_latency_ms"]) if r["avg_latency_ms"] else None
                ),
                "via_failover": int(r["via_failover"]),
            }
            for r in rows
        ],
    }


@router.get("/admin/usage/truncation-stats")
async def admin_usage_truncation_stats(
    days: int = 7,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """How often Mode 2 head-drop kicked in.

    Returns total trims, total dropped blocks, and a per-served_by
    breakdown so the operator can spot which fallback routes are
    most often forced to truncate (a signal that the alias's fallback
    has too small a context vs the typical request)."""
    _require_admin(principal)
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be between 1 and 90")

    start = datetime.now(timezone.utc) - timedelta(days=days)

    totals = (await db.execute(
        text("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE truncated_dropped > 0) AS truncated,
                COALESCE(SUM(truncated_dropped), 0) AS total_dropped
            FROM gateway_usage_events
            WHERE created_at >= :start
        """),
        {"start": start},
    )).mappings().first()
    total = int(totals["total"] or 0)
    truncated = int(totals["truncated"] or 0)
    rate = (truncated / total) if total > 0 else 0.0

    by_served = (await db.execute(
        text("""
            SELECT
                served_by,
                COUNT(*) AS truncated_requests,
                COALESCE(SUM(truncated_dropped), 0) AS total_dropped
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND truncated_dropped > 0
            GROUP BY served_by
            ORDER BY truncated_requests DESC
            LIMIT 20
        """),
        {"start": start},
    )).mappings().all()

    return {
        "days": days,
        "total_requests": total,
        "truncated_requests": truncated,
        "truncation_rate": round(rate, 4),
        "total_blocks_dropped": int(totals["total_dropped"] or 0),
        "by_served_by": [
            {
                "provider": r["served_by"],
                "truncated_requests": int(r["truncated_requests"]),
                "total_dropped": int(r["total_dropped"]),
            }
            for r in by_served
        ],
    }
