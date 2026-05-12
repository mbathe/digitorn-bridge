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

    # Cache + audio breakdown is NOT stored in QuotaCounter; pull straight
    # from the events log so the dashboard can show hit-rate, minutes
    # transcribed and audio cost share alongside the chat totals.
    breakdown = (await db.execute(
        text("""
            SELECT
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
                COALESCE(SUM(cache_write_tokens), 0) AS cache_write,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_total,
                COALESCE(SUM(audio_seconds), 0) AS audio_seconds,
                COUNT(*) FILTER (WHERE kind = 'transcription') AS transcriptions,
                COALESCE(SUM(total_cost_usd) FILTER (WHERE kind = 'transcription'), 0) AS audio_cost_usd
            FROM gateway_usage_events
            WHERE created_at >= :start
        """),
        {"start": month_start},
    )).mappings().first() or {}
    cache_read = int(breakdown.get("cache_read") or 0)
    cache_write = int(breakdown.get("cache_write") or 0)
    prompt_total = int(breakdown.get("prompt_total") or 0)
    audio_secs = float(breakdown.get("audio_seconds") or 0)
    transcriptions = int(breakdown.get("transcriptions") or 0)
    audio_cost = float(breakdown.get("audio_cost_usd") or 0)
    out["cache_read_tokens"] = float(cache_read)
    out["cache_write_tokens"] = float(cache_write)
    out["cache_hit_rate"] = round((cache_read / prompt_total) if prompt_total > 0 else 0.0, 4)
    out["audio_seconds"] = round(audio_secs, 2)
    out["audio_minutes"] = round(audio_secs / 60.0, 4)
    out["transcriptions"] = float(transcriptions)
    out["audio_cost_usd"] = round(audio_cost, 6)

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

    if metric not in (
        "requests", "messages", "tokens_input", "tokens_output",
        "tokens_total", "cost_usd", "cache_read_tokens", "cache_write_tokens",
        "audio_seconds", "audio_minutes", "transcriptions",
    ):
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
        "cache_read_tokens": "coalesce(sum(cache_read_tokens), 0)",
        "cache_write_tokens": "coalesce(sum(cache_write_tokens), 0)",
        "audio_seconds": "coalesce(sum(audio_seconds), 0)",
        "audio_minutes": "coalesce(sum(audio_seconds), 0) / 60.0",
        "transcriptions": "count(*) FILTER (WHERE kind = 'transcription')",
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
    (requests, tokens_total, cost_usd, cache breakdown) so the UI can
    flip between them without re-querying. Sorted by tokens_total desc.
    """
    _require_admin(principal)
    _validate_window(days, limit)

    start = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        text("""
            SELECT
                provider,
                COUNT(*) AS requests,
                COUNT(*) FILTER (WHERE kind = 'transcription') AS transcriptions,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(prompt_tokens), 0) AS tokens_input,
                COALESCE(SUM(completion_tokens), 0) AS tokens_output,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(audio_seconds), 0) AS audio_seconds,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                COUNT(DISTINCT user_id) AS users
            FROM gateway_usage_events
            WHERE created_at >= :start
            GROUP BY provider
            ORDER BY tokens_total DESC, audio_seconds DESC
            LIMIT :limit
        """),
        {"start": start, "limit": limit},
    )).mappings().all()

    out_rows = []
    for r in rows:
        cr = int(r["cache_read_tokens"])
        cw = int(r["cache_write_tokens"])
        ti = int(r["tokens_input"])
        secs = float(r["audio_seconds"] or 0)
        hit_rate = (cr / ti) if ti > 0 else 0.0
        out_rows.append({
            "provider": r["provider"] or "unknown",
            "requests": int(r["requests"]),
            "transcriptions": int(r["transcriptions"] or 0),
            "tokens_total": int(r["tokens_total"]),
            "tokens_input": ti,
            "tokens_output": int(r["tokens_output"]),
            "cache_read_tokens": cr,
            "cache_write_tokens": cw,
            "cache_hit_rate": round(hit_rate, 4),
            "audio_seconds": round(secs, 2),
            "audio_minutes": round(secs / 60.0, 4),
            "cost_usd": round(float(r["cost_usd"]), 6),
            "users": int(r["users"]),
        })
    return {"days": days, "providers": out_rows}


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
                COUNT(*) FILTER (WHERE kind = 'transcription') AS transcriptions,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(prompt_tokens), 0) AS tokens_input,
                COALESCE(SUM(completion_tokens), 0) AS tokens_output,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(audio_seconds), 0) AS audio_seconds,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                COUNT(DISTINCT user_id) AS users
            FROM gateway_usage_events
            WHERE created_at >= :start
            GROUP BY model
            ORDER BY tokens_total DESC, audio_seconds DESC
            LIMIT :limit
        """),
        {"start": start, "limit": limit},
    )).mappings().all()

    out_rows = []
    for r in rows:
        cr = int(r["cache_read_tokens"])
        cw = int(r["cache_write_tokens"])
        ti = int(r["tokens_input"])
        secs = float(r["audio_seconds"] or 0)
        hit_rate = (cr / ti) if ti > 0 else 0.0
        out_rows.append({
            "model": r["model"] or "unknown",
            "provider": r["provider"] or "unknown",
            "requests": int(r["requests"]),
            "transcriptions": int(r["transcriptions"] or 0),
            "tokens_total": int(r["tokens_total"]),
            "tokens_input": ti,
            "tokens_output": int(r["tokens_output"]),
            "cache_read_tokens": cr,
            "cache_write_tokens": cw,
            "cache_hit_rate": round(hit_rate, 4),
            "audio_seconds": round(secs, 2),
            "audio_minutes": round(secs / 60.0, 4),
            "cost_usd": round(float(r["cost_usd"]), 6),
            "users": int(r["users"]),
        })
    return {"days": days, "models": out_rows}


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


# ── Per-attribution analytics (app / agent / session / run) ─────────
#
# Granular breakdown by the 4 attribution IDs the daemon forwards on
# every dispatch (X-Digitorn-App-Id, X-Digitorn-Agent-Id,
# X-Digitorn-Session-Id, X-Digitorn-Run-Id). Powers a per-user
# dashboard view: "which apps consume my budget", "which agent in app
# X cost the most this week", "show me the timeline of session Y for
# debugging".
#
# Performance: all queries hit the partial indexes added by migration
# 0016 (``ix_gateway_usage_events_app``, ``..._session``, ``..._run``).
# Expected p95 < 100ms even on multi-million-row partitions.
#
# These endpoints DO NOT touch the gateway hot path. They run against
# Postgres in the admin context, and the dispatch BackgroundTask
# writes use append-only inserts on the same table; MVCC means reads
# never block writes.


def _validate_attr_window(days: int) -> None:
    if days < 1 or days > 365:
        raise HTTPException(400, detail="days must be between 1 and 365")


@router.get("/admin/usage/by-app")
async def admin_usage_by_app(
    days: int = 30,
    limit: int = 50,
    user_id: str | None = None,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Top apps over the window. Optional ``user_id`` scopes to one
    user (per-user-per-app breakdown). Returns ordered by tokens_total
    desc with cost / requests / users(if not user-scoped) / agents."""
    _require_admin(principal)
    _validate_attr_window(days)
    if limit < 1 or limit > 200:
        raise HTTPException(400, detail="limit must be between 1 and 200")

    start = datetime.now(timezone.utc) - timedelta(days=days)
    where_user = "AND user_id = :user_id" if user_id else ""
    rows = (await db.execute(
        text(f"""
            SELECT
                app_id,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(prompt_tokens), 0) AS tokens_input,
                COALESCE(SUM(completion_tokens), 0) AS tokens_output,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                COUNT(DISTINCT user_id) AS users,
                COUNT(DISTINCT agent_id) FILTER (WHERE agent_id IS NOT NULL) AS agents,
                COUNT(DISTINCT external_sid) FILTER (WHERE external_sid IS NOT NULL) AS sessions,
                AVG(latency_ms) AS avg_latency_ms,
                MIN(created_at) AS first_seen_at,
                MAX(created_at) AS last_seen_at
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND app_id IS NOT NULL
              {where_user}
            GROUP BY app_id
            ORDER BY tokens_total DESC
            LIMIT :limit
        """),
        {"start": start, "limit": limit,
         **({"user_id": user_id} if user_id else {})},
    )).mappings().all()
    return {
        "days": days,
        "user_id": user_id,
        "apps": [
            {
                "app_id": r["app_id"],
                "requests": int(r["requests"]),
                "tokens_total": int(r["tokens_total"]),
                "tokens_input": int(r["tokens_input"]),
                "tokens_output": int(r["tokens_output"]),
                "cost_usd": round(float(r["cost_usd"]), 6),
                "users": int(r["users"]),
                "agents": int(r["agents"]),
                "sessions": int(r["sessions"]),
                "avg_latency_ms": (
                    int(r["avg_latency_ms"]) if r["avg_latency_ms"] else None
                ),
                "first_seen_at": r["first_seen_at"].isoformat() if r["first_seen_at"] else None,
                "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/admin/usage/by-app/{app_id}/users")
async def admin_usage_app_users(
    app_id: str,
    days: int = 30,
    limit: int = 50,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Top users of a single app. Use case: "who is consuming my
    budget on app X this month". Sorted by cost_usd desc."""
    _require_admin(principal)
    _validate_attr_window(days)
    if limit < 1 or limit > 200:
        raise HTTPException(400, detail="limit must be between 1 and 200")

    start = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        text("""
            SELECT
                user_id,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                COUNT(DISTINCT external_sid) FILTER (WHERE external_sid IS NOT NULL) AS sessions,
                MAX(created_at) AS last_seen_at
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND app_id = :app_id
            GROUP BY user_id
            ORDER BY cost_usd DESC
            LIMIT :limit
        """),
        {"start": start, "app_id": app_id, "limit": limit},
    )).mappings().all()
    return {
        "days": days,
        "app_id": app_id,
        "users": [
            {
                "user_id": r["user_id"],
                "requests": int(r["requests"]),
                "tokens_total": int(r["tokens_total"]),
                "cost_usd": round(float(r["cost_usd"]), 6),
                "sessions": int(r["sessions"]),
                "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/admin/usage/by-app/{app_id}/agents")
async def admin_usage_app_agents(
    app_id: str,
    days: int = 30,
    limit: int = 50,
    user_id: str | None = None,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Top agents (and sub-agents) for an app. Optional ``user_id``
    scopes to one user. Use case: "which agent in app X is the most
    expensive" or "which sub-agent of my run is hottest"."""
    _require_admin(principal)
    _validate_attr_window(days)
    if limit < 1 or limit > 200:
        raise HTTPException(400, detail="limit must be between 1 and 200")

    start = datetime.now(timezone.utc) - timedelta(days=days)
    where_user = "AND user_id = :user_id" if user_id else ""
    rows = (await db.execute(
        text(f"""
            SELECT
                agent_id,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                COUNT(DISTINCT user_id) AS users,
                COUNT(DISTINCT external_sid) FILTER (WHERE external_sid IS NOT NULL) AS sessions,
                AVG(latency_ms) AS avg_latency_ms,
                MAX(created_at) AS last_seen_at
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND app_id = :app_id
              AND agent_id IS NOT NULL
              {where_user}
            GROUP BY agent_id
            ORDER BY cost_usd DESC
            LIMIT :limit
        """),
        {"start": start, "app_id": app_id, "limit": limit,
         **({"user_id": user_id} if user_id else {})},
    )).mappings().all()
    return {
        "days": days,
        "app_id": app_id,
        "user_id": user_id,
        "agents": [
            {
                "agent_id": r["agent_id"],
                "requests": int(r["requests"]),
                "tokens_total": int(r["tokens_total"]),
                "cost_usd": round(float(r["cost_usd"]), 6),
                "users": int(r["users"]),
                "sessions": int(r["sessions"]),
                "avg_latency_ms": (
                    int(r["avg_latency_ms"]) if r["avg_latency_ms"] else None
                ),
                "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/admin/usage/by-user/{user_id}/apps")
async def admin_usage_user_apps(
    user_id: str,
    days: int = 30,
    limit: int = 50,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Apps a single user has consumed in the window. Use case: the
    user's own dashboard - "where did my budget go". Sorted by
    cost_usd desc."""
    _require_admin(principal)
    _validate_attr_window(days)
    if limit < 1 or limit > 200:
        raise HTTPException(400, detail="limit must be between 1 and 200")

    start = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        text("""
            SELECT
                app_id,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                COUNT(DISTINCT external_sid) FILTER (WHERE external_sid IS NOT NULL) AS sessions,
                COUNT(DISTINCT agent_id) FILTER (WHERE agent_id IS NOT NULL) AS agents,
                MAX(created_at) AS last_seen_at
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND user_id = :user_id
              AND app_id IS NOT NULL
            GROUP BY app_id
            ORDER BY cost_usd DESC
            LIMIT :limit
        """),
        {"start": start, "user_id": user_id, "limit": limit},
    )).mappings().all()
    return {
        "days": days,
        "user_id": user_id,
        "apps": [
            {
                "app_id": r["app_id"],
                "requests": int(r["requests"]),
                "tokens_total": int(r["tokens_total"]),
                "cost_usd": round(float(r["cost_usd"]), 6),
                "sessions": int(r["sessions"]),
                "agents": int(r["agents"]),
                "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/admin/usage/sessions/{session_id}")
async def admin_usage_session_detail(
    session_id: str,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Full timeline of a single session, ordered by created_at.
    Each row carries provider / model / tokens / cost / latency /
    served_by / cache_hit / failover_trail. Use case: "show me what
    happened in session X" debugging. Capped at 1000 rows to avoid
    pulling a runaway log into the dashboard."""
    _require_admin(principal)
    if not session_id or len(session_id) > 256:
        raise HTTPException(400, detail="invalid session_id")

    rows = (await db.execute(
        text("""
            SELECT
                id, created_at, user_id, app_id, agent_id, run_id,
                provider, model, served_by, kind,
                prompt_tokens, completion_tokens, total_cost_usd,
                latency_ms, error_class, attempts, failover_trail,
                truncated_dropped, cache_hit
            FROM gateway_usage_events
            WHERE external_sid = :sid
            ORDER BY created_at
            LIMIT 1000
        """),
        {"sid": session_id},
    )).mappings().all()

    if not rows:
        return {"session_id": session_id, "events": [], "summary": {
            "total_requests": 0, "total_tokens": 0, "total_cost_usd": 0.0,
        }}

    return {
        "session_id": session_id,
        "events": [
            {
                "id": str(r["id"]),
                "ts": r["created_at"].isoformat() if r["created_at"] else None,
                "user_id": r["user_id"],
                "app_id": r["app_id"],
                "agent_id": r["agent_id"],
                "run_id": r["run_id"],
                "provider": r["provider"],
                "model": r["model"],
                "served_by": r["served_by"],
                "kind": r["kind"],
                "prompt_tokens": int(r["prompt_tokens"] or 0),
                "completion_tokens": int(r["completion_tokens"] or 0),
                "cost_usd": round(float(r["total_cost_usd"] or 0.0), 6),
                "latency_ms": int(r["latency_ms"]) if r["latency_ms"] else None,
                "error_class": r["error_class"],
                "attempts": int(r["attempts"] or 1),
                "failover_trail": r["failover_trail"],
                "truncated_dropped": int(r["truncated_dropped"] or 0),
                "cache_hit": bool(r["cache_hit"]),
            }
            for r in rows
        ],
        "summary": {
            "total_requests": len(rows),
            "total_tokens": sum(int((r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0)) for r in rows),
            "total_cost_usd": round(sum(float(r["total_cost_usd"] or 0.0) for r in rows), 6),
            "first_ts": rows[0]["created_at"].isoformat() if rows[0]["created_at"] else None,
            "last_ts": rows[-1]["created_at"].isoformat() if rows[-1]["created_at"] else None,
            "distinct_agents": len({r["agent_id"] for r in rows if r["agent_id"]}),
            "distinct_runs": len({r["run_id"] for r in rows if r["run_id"]}),
            "cache_hits": sum(1 for r in rows if r["cache_hit"]),
            "failovers": sum(1 for r in rows if int(r["attempts"] or 1) > 1),
        },
    }


@router.get("/admin/usage/runs/{run_id}")
async def admin_usage_run_detail(
    run_id: str,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Detail of a single run (typically one agent_loop turn from the
    daemon's perspective). Same shape as session detail but scoped to
    a single run_id - typically a few rows for the lead agent +
    spawned sub-agents."""
    _require_admin(principal)
    if not run_id or len(run_id) > 256:
        raise HTTPException(400, detail="invalid run_id")

    rows = (await db.execute(
        text("""
            SELECT
                id, created_at, user_id, app_id, agent_id, external_sid,
                provider, model, served_by, kind,
                prompt_tokens, completion_tokens, total_cost_usd,
                latency_ms, error_class, attempts, failover_trail,
                truncated_dropped, cache_hit
            FROM gateway_usage_events
            WHERE run_id = :rid
            ORDER BY created_at
            LIMIT 1000
        """),
        {"rid": run_id},
    )).mappings().all()

    if not rows:
        return {"run_id": run_id, "events": [], "summary": {
            "total_requests": 0, "total_tokens": 0, "total_cost_usd": 0.0,
        }}

    return {
        "run_id": run_id,
        "events": [
            {
                "id": str(r["id"]),
                "ts": r["created_at"].isoformat() if r["created_at"] else None,
                "user_id": r["user_id"],
                "app_id": r["app_id"],
                "agent_id": r["agent_id"],
                "session_id": r["external_sid"],
                "provider": r["provider"],
                "model": r["model"],
                "served_by": r["served_by"],
                "kind": r["kind"],
                "prompt_tokens": int(r["prompt_tokens"] or 0),
                "completion_tokens": int(r["completion_tokens"] or 0),
                "cost_usd": round(float(r["total_cost_usd"] or 0.0), 6),
                "latency_ms": int(r["latency_ms"]) if r["latency_ms"] else None,
                "error_class": r["error_class"],
                "attempts": int(r["attempts"] or 1),
                "failover_trail": r["failover_trail"],
                "truncated_dropped": int(r["truncated_dropped"] or 0),
                "cache_hit": bool(r["cache_hit"]),
            }
            for r in rows
        ],
        "summary": {
            "total_requests": len(rows),
            "total_tokens": sum(int((r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0)) for r in rows),
            "total_cost_usd": round(sum(float(r["total_cost_usd"] or 0.0) for r in rows), 6),
            "first_ts": rows[0]["created_at"].isoformat() if rows[0]["created_at"] else None,
            "last_ts": rows[-1]["created_at"].isoformat() if rows[-1]["created_at"] else None,
            "distinct_agents": len({r["agent_id"] for r in rows if r["agent_id"]}),
            "cache_hits": sum(1 for r in rows if r["cache_hit"]),
            "failovers": sum(1 for r in rows if int(r["attempts"] or 1) > 1),
        },
    }


# ── Live view (last 60s aggregation) ────────────────────────────────


@router.get("/admin/usage/live")
async def admin_usage_live(
    window_seconds: int = 60,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Real-time activity over the last N seconds (default 60). Backed
    by an indexed range scan on ``created_at``; sub-100ms even at
    1000 rps. The dispatch hot path is NOT involved - this is a pure
    read endpoint, MVCC isolation means no write locks ever block it.

    Returns the rps, top providers right now, top users right now,
    p50/p95 latency, and counts of in-flight credentials by tier."""
    _require_admin(principal)
    if window_seconds < 5 or window_seconds > 300:
        raise HTTPException(400, detail="window_seconds must be between 5 and 300")

    start = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)

    # Headline counts.
    head = (await db.execute(
        text("""
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                COUNT(DISTINCT user_id) AS active_users,
                COUNT(DISTINCT external_sid) FILTER (WHERE external_sid IS NOT NULL) AS active_sessions,
                COUNT(*) FILTER (WHERE error_class IS NOT NULL AND error_class != 'cache_hit') AS errors,
                COUNT(*) FILTER (WHERE cache_hit) AS cache_hits,
                COUNT(*) FILTER (WHERE attempts > 1) AS failover_requests,
                AVG(latency_ms) AS avg_latency_ms,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
                PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99
            FROM gateway_usage_events
            WHERE created_at >= :start
        """),
        {"start": start},
    )).mappings().first()

    # Top served-by RIGHT NOW.
    top_served = (await db.execute(
        text("""
            SELECT served_by, COUNT(*) AS n
            FROM gateway_usage_events
            WHERE created_at >= :start AND served_by IS NOT NULL
            GROUP BY served_by ORDER BY n DESC LIMIT 8
        """),
        {"start": start},
    )).mappings().all()

    # Top users RIGHT NOW.
    top_users = (await db.execute(
        text("""
            SELECT user_id, COUNT(*) AS n,
                   COALESCE(SUM(total_cost_usd), 0) AS cost_usd
            FROM gateway_usage_events
            WHERE created_at >= :start
            GROUP BY user_id ORDER BY n DESC LIMIT 8
        """),
        {"start": start},
    )).mappings().all()

    total = int(head["requests"] or 0)
    return {
        "window_seconds": window_seconds,
        "now": datetime.now(timezone.utc).isoformat(),
        "rps": round(total / max(window_seconds, 1), 2),
        "requests": total,
        "tokens_total": int(head["tokens_total"] or 0),
        "cost_usd": round(float(head["cost_usd"] or 0), 6),
        "active_users": int(head["active_users"] or 0),
        "active_sessions": int(head["active_sessions"] or 0),
        "errors": int(head["errors"] or 0),
        "cache_hits": int(head["cache_hits"] or 0),
        "failover_requests": int(head["failover_requests"] or 0),
        "avg_latency_ms": int(head["avg_latency_ms"]) if head["avg_latency_ms"] else None,
        "p50_latency_ms": int(head["p50"]) if head["p50"] else None,
        "p95_latency_ms": int(head["p95"]) if head["p95"] else None,
        "p99_latency_ms": int(head["p99"]) if head["p99"] else None,
        "top_served_by": [
            {"provider": r["served_by"], "requests": int(r["n"])}
            for r in top_served
        ],
        "top_users": [
            {
                "user_id": r["user_id"],
                "requests": int(r["n"]),
                "cost_usd": round(float(r["cost_usd"] or 0), 6),
            }
            for r in top_users
        ],
    }


# ── Cost forecasting per user ───────────────────────────────────────


@router.get("/admin/usage/forecast/{user_id}")
async def admin_usage_forecast(
    user_id: str,
    days_lookback: int = 7,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Linear burn-rate projection for one user over a configurable
    lookback window. Returns:
      - daily_avg_cost_usd / daily_avg_tokens
      - projected_eom_cost_usd: extrapolated end-of-month cost at this rate
      - days_remaining_in_month (calendar)
      - per-day series (raw points to chart the trend)

    No quota lookup here - the dashboard combines this with
    /v1/quota/me on the user side to surface "you'll hit your plan
    cap in N days at this rate"."""
    _require_admin(principal)
    if days_lookback < 1 or days_lookback > 90:
        raise HTTPException(400, detail="days_lookback must be 1-90")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_lookback)

    # Per-day series + headline.
    rows = (await db.execute(
        text("""
            SELECT
                date_trunc('day', created_at) AS day,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd
            FROM gateway_usage_events
            WHERE user_id = :uid AND created_at >= :start
            GROUP BY day ORDER BY day
        """),
        {"uid": user_id, "start": start},
    )).mappings().all()

    if not rows:
        return {
            "user_id": user_id,
            "days_lookback": days_lookback,
            "daily_avg_cost_usd": 0.0,
            "daily_avg_tokens": 0,
            "daily_avg_requests": 0,
            "projected_eom_cost_usd": 0.0,
            "days_remaining_in_month": _days_remaining_in_month(now),
            "series": [],
        }

    total_cost = sum(float(r["cost_usd"] or 0) for r in rows)
    total_tokens = sum(int(r["tokens"] or 0) for r in rows)
    total_reqs = sum(int(r["requests"] or 0) for r in rows)
    n = len(rows)
    avg_cost = total_cost / n
    avg_tokens = total_tokens / n
    avg_reqs = total_reqs / n
    days_remaining = _days_remaining_in_month(now)

    # Month-to-date cost so far.
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mtd = (await db.execute(
        text("""
            SELECT COALESCE(SUM(total_cost_usd), 0) AS mtd
            FROM gateway_usage_events
            WHERE user_id = :uid AND created_at >= :ms
        """),
        {"uid": user_id, "ms": month_start},
    )).mappings().first()
    mtd_cost = round(float(mtd["mtd"] or 0), 6)

    return {
        "user_id": user_id,
        "days_lookback": days_lookback,
        "daily_avg_cost_usd": round(avg_cost, 6),
        "daily_avg_tokens": int(avg_tokens),
        "daily_avg_requests": int(avg_reqs),
        "month_to_date_cost_usd": mtd_cost,
        "days_remaining_in_month": days_remaining,
        "projected_eom_cost_usd": round(mtd_cost + avg_cost * days_remaining, 6),
        "series": [
            {
                "day": r["day"].date().isoformat(),
                "requests": int(r["requests"]),
                "tokens": int(r["tokens"]),
                "cost_usd": round(float(r["cost_usd"] or 0), 6),
            }
            for r in rows
        ],
    }


def _days_remaining_in_month(now: datetime) -> int:
    """Whole days left until the 1st of next month (00:00 UTC)."""
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return max(0, (next_month - now).days)


# ── Conversation depth per app ──────────────────────────────────────


@router.get("/admin/usage/conversations")
async def admin_usage_conversations(
    days: int = 7,
    app_id: str | None = None,
    user_id: str | None = None,
    limit: int = 50,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Conversation-depth metrics: events per session distribution.
    Lets the dashboard answer 'are agents looping' / 'how deep does
    a typical conversation go on this app'.

    Optional ``app_id`` and ``user_id`` filters. Returns:
      - total_sessions, single_message_sessions
      - avg / median / p95 / max events per session
      - per-app breakdown (top apps by depth) when app_id is None
    """
    _require_admin(principal)
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be 1-90")
    if limit < 1 or limit > 200:
        raise HTTPException(400, detail="limit must be 1-200")

    start = datetime.now(timezone.utc) - timedelta(days=days)

    where_filters = ["created_at >= :start", "external_sid IS NOT NULL"]
    params: dict[str, Any] = {"start": start, "limit": limit}
    if app_id:
        where_filters.append("app_id = :app_id")
        params["app_id"] = app_id
    if user_id:
        where_filters.append("user_id = :user_id")
        params["user_id"] = user_id
    where_sql = " AND ".join(where_filters)

    # Per-session event counts.
    head = (await db.execute(
        text(f"""
            WITH per_sess AS (
                SELECT external_sid, COUNT(*) AS events
                FROM gateway_usage_events
                WHERE {where_sql}
                GROUP BY external_sid
            )
            SELECT
                COUNT(*) AS total_sessions,
                COUNT(*) FILTER (WHERE events = 1) AS single_event_sessions,
                COALESCE(AVG(events), 0) AS avg_events,
                COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY events), 0) AS median_events,
                COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY events), 0) AS p95_events,
                COALESCE(MAX(events), 0) AS max_events
            FROM per_sess
        """),
        params,
    )).mappings().first()

    # Optional per-app breakdown (only when app_id NOT specified).
    by_app: list[dict[str, Any]] = []
    if not app_id:
        rows = (await db.execute(
            text(f"""
                WITH per_sess AS (
                    SELECT app_id, external_sid, COUNT(*) AS events
                    FROM gateway_usage_events
                    WHERE {where_sql} AND app_id IS NOT NULL
                    GROUP BY app_id, external_sid
                )
                SELECT
                    app_id,
                    COUNT(*) AS sessions,
                    COALESCE(AVG(events), 0) AS avg_events,
                    COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY events), 0) AS p95_events,
                    COALESCE(MAX(events), 0) AS max_events
                FROM per_sess
                GROUP BY app_id
                ORDER BY avg_events DESC
                LIMIT :limit
            """),
            params,
        )).mappings().all()
        by_app = [
            {
                "app_id": r["app_id"],
                "sessions": int(r["sessions"]),
                "avg_events_per_session": round(float(r["avg_events"]), 2),
                "p95_events_per_session": int(r["p95_events"]),
                "max_events_per_session": int(r["max_events"]),
            }
            for r in rows
        ]

    return {
        "days": days,
        "app_id": app_id,
        "user_id": user_id,
        "total_sessions": int(head["total_sessions"] or 0),
        "single_event_sessions": int(head["single_event_sessions"] or 0),
        "avg_events_per_session": round(float(head["avg_events"] or 0), 2),
        "median_events_per_session": float(head["median_events"] or 0),
        "p95_events_per_session": int(head["p95_events"] or 0),
        "max_events_per_session": int(head["max_events"] or 0),
        "by_app": by_app,
    }


# ── Sessions list (recent activity index) ───────────────────────────


@router.get("/admin/usage/sessions")
async def admin_usage_sessions_list(
    days: int = 7,
    limit: int = 50,
    user_id: str | None = None,
    app_id: str | None = None,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Recent sessions across the cluster, ordered by most recent first.
    Powers a `/sessions` index page so operators can browse without
    knowing IDs ahead of time. Optional ``user_id`` / ``app_id``
    filters narrow the view.

    Returns one row per distinct external_sid with summary metrics.
    Cap: 200 (anti-runaway). Indexed via ``ix_gateway_usage_events_session``.
    """
    _require_admin(principal)
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be 1-90")
    if limit < 1 or limit > 200:
        raise HTTPException(400, detail="limit must be 1-200")

    start = datetime.now(timezone.utc) - timedelta(days=days)
    where = ["created_at >= :start", "external_sid IS NOT NULL"]
    params: dict[str, Any] = {"start": start, "limit": limit}
    if user_id:
        where.append("user_id = :user_id")
        params["user_id"] = user_id
    if app_id:
        where.append("app_id = :app_id")
        params["app_id"] = app_id
    where_sql = " AND ".join(where)

    rows = (await db.execute(
        text(f"""
            SELECT
                external_sid,
                MAX(created_at) AS last_seen_at,
                MIN(created_at) AS first_seen_at,
                COUNT(*) AS events,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                MAX(user_id) AS user_id,
                MAX(app_id) AS app_id,
                COUNT(DISTINCT agent_id) FILTER (WHERE agent_id IS NOT NULL) AS distinct_agents,
                COUNT(*) FILTER (WHERE error_class IS NOT NULL AND error_class != 'cache_hit') AS errors,
                COUNT(*) FILTER (WHERE cache_hit) AS cache_hits,
                COUNT(*) FILTER (WHERE attempts > 1) AS failovers
            FROM gateway_usage_events
            WHERE {where_sql}
            GROUP BY external_sid
            ORDER BY last_seen_at DESC
            LIMIT :limit
        """),
        params,
    )).mappings().all()

    return {
        "days": days,
        "user_id": user_id,
        "app_id": app_id,
        "sessions": [
            {
                "session_id": r["external_sid"],
                "user_id": r["user_id"],
                "app_id": r["app_id"],
                "first_seen_at": r["first_seen_at"].isoformat() if r["first_seen_at"] else None,
                "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
                "events": int(r["events"]),
                "tokens": int(r["tokens"]),
                "cost_usd": round(float(r["cost_usd"] or 0), 6),
                "distinct_agents": int(r["distinct_agents"] or 0),
                "errors": int(r["errors"]),
                "cache_hits": int(r["cache_hits"]),
                "failovers": int(r["failovers"]),
            }
            for r in rows
        ],
    }


# ── Anomaly detection (z-score on recent burn rate) ────────────────


@router.get("/admin/usage/anomalies")
async def admin_usage_anomalies(
    days_baseline: int = 14,
    z_threshold: float = 2.0,
    limit: int = 50,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Users whose YESTERDAY consumption is statistically anomalous vs
    their own ``days_baseline``-day rolling baseline. Flags both:

      * SPIKE: yesterday's cost > baseline_mean + z_threshold * baseline_stddev
      * IDLE:  user normally active but yesterday = 0 events (potential
              service issue or churn signal)

    Pure-SQL using window functions; no per-row Python work.
    Heavy enough to NOT run on every dashboard tick - caller polls
    every minute or so.
    """
    _require_admin(principal)
    if days_baseline < 3 or days_baseline > 90:
        raise HTTPException(400, detail="days_baseline must be 3-90")
    if z_threshold < 0.5 or z_threshold > 10:
        raise HTTPException(400, detail="z_threshold must be 0.5-10")
    if limit < 1 or limit > 200:
        raise HTTPException(400, detail="limit must be 1-200")

    now = datetime.now(timezone.utc)
    yesterday_start = (now - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    yesterday_end = yesterday_start + timedelta(days=1)
    baseline_start = yesterday_start - timedelta(days=days_baseline)

    rows = (await db.execute(
        text("""
            WITH per_user_daily AS (
                SELECT
                    user_id,
                    date_trunc('day', created_at) AS day,
                    COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                    COUNT(*) AS requests
                FROM gateway_usage_events
                WHERE created_at >= :baseline_start
                  AND created_at < :yesterday_end
                  AND user_id IS NOT NULL
                GROUP BY user_id, day
            ),
            baseline AS (
                SELECT
                    user_id,
                    AVG(cost_usd) AS mean_cost,
                    STDDEV_POP(cost_usd) AS std_cost,
                    AVG(requests) AS mean_reqs,
                    COUNT(*) AS active_days
                FROM per_user_daily
                WHERE day < :yesterday_start
                GROUP BY user_id
                HAVING COUNT(*) >= 3
            ),
            yesterday AS (
                SELECT user_id, cost_usd AS y_cost, requests AS y_reqs
                FROM per_user_daily
                WHERE day = :yesterday_start
            )
            SELECT
                b.user_id,
                b.mean_cost,
                b.std_cost,
                b.mean_reqs,
                b.active_days,
                COALESCE(y.y_cost, 0) AS y_cost,
                COALESCE(y.y_reqs, 0) AS y_reqs,
                CASE
                    WHEN b.std_cost > 0 THEN
                        (COALESCE(y.y_cost, 0) - b.mean_cost) / b.std_cost
                    ELSE NULL
                END AS z_score
            FROM baseline b
            LEFT JOIN yesterday y ON y.user_id = b.user_id
            WHERE
                (b.std_cost > 0
                 AND ABS((COALESCE(y.y_cost, 0) - b.mean_cost) / b.std_cost) >= :z)
                OR (b.mean_reqs > 5 AND COALESCE(y.y_reqs, 0) = 0)
            ORDER BY ABS(
                CASE WHEN b.std_cost > 0
                     THEN (COALESCE(y.y_cost, 0) - b.mean_cost) / b.std_cost
                     ELSE 99 END
            ) DESC
            LIMIT :limit
        """),
        {
            "baseline_start": baseline_start,
            "yesterday_start": yesterday_start,
            "yesterday_end": yesterday_end,
            "z": z_threshold,
            "limit": limit,
        },
    )).mappings().all()

    anomalies: list[dict[str, Any]] = []
    for r in rows:
        z = float(r["z_score"]) if r["z_score"] is not None else None
        kind = "idle" if (r["y_reqs"] == 0 and r["mean_reqs"] > 5) else (
            "spike" if (z is not None and z > 0) else "drop"
        )
        anomalies.append({
            "user_id": r["user_id"],
            "kind": kind,
            "z_score": round(z, 2) if z is not None else None,
            "yesterday_cost_usd": round(float(r["y_cost"] or 0), 6),
            "yesterday_requests": int(r["y_reqs"] or 0),
            "baseline_mean_cost_usd": round(float(r["mean_cost"] or 0), 6),
            "baseline_std_cost_usd": round(float(r["std_cost"] or 0), 6),
            "baseline_mean_requests": round(float(r["mean_reqs"] or 0), 1),
            "baseline_active_days": int(r["active_days"] or 0),
        })

    return {
        "days_baseline": days_baseline,
        "z_threshold": z_threshold,
        "yesterday_date": yesterday_start.date().isoformat(),
        "anomalies": anomalies,
    }


# ── User-facing /v1/usage/me/* (non-admin, scoped to caller's JWT) ──
#
# Mirror of the admin endpoints but ALWAYS scoped to the caller's
# user_id (extracted from JWT). No admin role required - any
# authenticated user can hit these to see their own consumption.
# This is what powers a "my dashboard" view in the daemon UI / web.


@router.get("/v1/usage/me/apps")
async def me_usage_apps(
    days: int = 30,
    limit: int = 50,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Apps the caller has consumed in the window. Same shape as
    /admin/usage/by-user/{id}/apps but scoped to the JWT user."""
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be 1-90")
    if limit < 1 or limit > 200:
        raise HTTPException(400, detail="limit must be 1-200")

    start = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        text("""
            SELECT
                app_id,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_total,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                COUNT(DISTINCT external_sid) FILTER (WHERE external_sid IS NOT NULL) AS sessions,
                COUNT(DISTINCT agent_id) FILTER (WHERE agent_id IS NOT NULL) AS agents,
                MAX(created_at) AS last_seen_at
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND user_id = :uid
              AND app_id IS NOT NULL
            GROUP BY app_id
            ORDER BY cost_usd DESC
            LIMIT :limit
        """),
        {"start": start, "uid": principal.user_id, "limit": limit},
    )).mappings().all()
    return {
        "days": days,
        "user_id": principal.user_id,
        "apps": [
            {
                "app_id": r["app_id"],
                "requests": int(r["requests"]),
                "tokens_total": int(r["tokens_total"]),
                "cost_usd": round(float(r["cost_usd"]), 6),
                "sessions": int(r["sessions"]),
                "agents": int(r["agents"]),
                "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/v1/usage/me/forecast")
async def me_usage_forecast(
    days_lookback: int = 7,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Forecast for the caller. Same shape as the admin variant but
    no role check, scoped to JWT user."""
    if days_lookback < 1 or days_lookback > 90:
        raise HTTPException(400, detail="days_lookback must be 1-90")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_lookback)
    rows = (await db.execute(
        text("""
            SELECT
                date_trunc('day', created_at) AS day,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd
            FROM gateway_usage_events
            WHERE user_id = :uid AND created_at >= :start
            GROUP BY day ORDER BY day
        """),
        {"uid": principal.user_id, "start": start},
    )).mappings().all()

    days_remaining = _days_remaining_in_month(now)
    if not rows:
        return {
            "user_id": principal.user_id,
            "days_lookback": days_lookback,
            "daily_avg_cost_usd": 0.0,
            "daily_avg_tokens": 0,
            "daily_avg_requests": 0,
            "month_to_date_cost_usd": 0.0,
            "days_remaining_in_month": days_remaining,
            "projected_eom_cost_usd": 0.0,
            "series": [],
        }

    total_cost = sum(float(r["cost_usd"] or 0) for r in rows)
    total_tokens = sum(int(r["tokens"] or 0) for r in rows)
    total_reqs = sum(int(r["requests"] or 0) for r in rows)
    n = len(rows)
    avg_cost = total_cost / n
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mtd = (await db.execute(
        text("""
            SELECT COALESCE(SUM(total_cost_usd), 0) AS mtd
            FROM gateway_usage_events
            WHERE user_id = :uid AND created_at >= :ms
        """),
        {"uid": principal.user_id, "ms": month_start},
    )).mappings().first()
    mtd_cost = round(float(mtd["mtd"] or 0), 6)

    return {
        "user_id": principal.user_id,
        "days_lookback": days_lookback,
        "daily_avg_cost_usd": round(avg_cost, 6),
        "daily_avg_tokens": int(total_tokens / n),
        "daily_avg_requests": int(total_reqs / n),
        "month_to_date_cost_usd": mtd_cost,
        "days_remaining_in_month": days_remaining,
        "projected_eom_cost_usd": round(mtd_cost + avg_cost * days_remaining, 6),
        "series": [
            {
                "day": r["day"].date().isoformat(),
                "requests": int(r["requests"]),
                "tokens": int(r["tokens"]),
                "cost_usd": round(float(r["cost_usd"] or 0), 6),
            }
            for r in rows
        ],
    }


# ── Pivot table (configurable rows × columns × aggregate) ──────────


# Whitelist of dimensions a caller can ask to pivot on. Keeps the
# endpoint free of arbitrary SQL while still very flexible.
_PIVOT_DIMS: dict[str, str] = {
    # row/column choices
    "user_id": "user_id",
    "app_id": "app_id",
    "agent_id": "agent_id",
    "model": "model",
    "provider": "provider",
    "served_by": "served_by",
    # time buckets (column-only typically)
    "day": "date_trunc('day', created_at)::text",
    "hour": "date_trunc('hour', created_at)::text",
    "week": "date_trunc('week', created_at)::text",
    "month": "date_trunc('month', created_at)::text",
}

# Whitelisted value expressions + aggregation function. Composable.
_PIVOT_VALUES: dict[str, str] = {
    "requests": "COUNT(*)",
    "tokens_total": "COALESCE(SUM(prompt_tokens + completion_tokens), 0)",
    "tokens_input": "COALESCE(SUM(prompt_tokens), 0)",
    "tokens_output": "COALESCE(SUM(completion_tokens), 0)",
    "cost_usd": "COALESCE(SUM(total_cost_usd), 0)",
    "avg_latency_ms": "COALESCE(AVG(latency_ms), 0)",
    "errors": "COUNT(*) FILTER (WHERE error_class IS NOT NULL AND error_class != 'cache_hit')",
    "cache_hits": "COUNT(*) FILTER (WHERE cache_hit)",
    "failovers": "COUNT(*) FILTER (WHERE attempts > 1)",
}


@router.get("/admin/usage/pivot")
async def admin_usage_pivot(
    row: str,
    col: str,
    value: str = "requests",
    days: int = 7,
    max_rows: int = 50,
    max_cols: int = 50,
    user_id: str | None = None,
    app_id: str | None = None,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Dynamic pivot table over ``gateway_usage_events``.

    rows × cols × value aggregation. All dimensions and value
    expressions come from a strict whitelist; nothing the caller
    sends is interpolated raw into SQL.

    Cap: ``max_rows × max_cols`` cells (default 2500). Anything past
    the cap is dropped from the result.

    Use cases:
      * who spends what on which apps -> row=user_id col=app_id value=cost_usd
      * which providers serve what models -> row=provider col=model value=requests
      * traffic over time per app -> row=app_id col=day value=requests
      * error rate per provider over time -> row=provider col=day value=errors
    """
    _require_admin(principal)
    if days < 1 or days > 365:
        raise HTTPException(400, detail="days must be 1-365")
    if max_rows < 1 or max_rows > 200:
        raise HTTPException(400, detail="max_rows must be 1-200")
    if max_cols < 1 or max_cols > 200:
        raise HTTPException(400, detail="max_cols must be 1-200")
    if row not in _PIVOT_DIMS or col not in _PIVOT_DIMS:
        raise HTTPException(
            400,
            detail=f"row/col must be one of {sorted(_PIVOT_DIMS.keys())}",
        )
    if row == col:
        raise HTTPException(400, detail="row and col must differ")
    if value not in _PIVOT_VALUES:
        raise HTTPException(
            400,
            detail=f"value must be one of {sorted(_PIVOT_VALUES.keys())}",
        )

    row_expr = _PIVOT_DIMS[row]
    col_expr = _PIVOT_DIMS[col]
    val_expr = _PIVOT_VALUES[value]

    start = datetime.now(timezone.utc) - timedelta(days=days)

    # WHERE clause: drop NULL row / col automatically (those would
    # collapse into a single noisy bucket).
    where_filters = [
        "created_at >= :start",
        f"{row_expr} IS NOT NULL",
        f"{col_expr} IS NOT NULL",
    ]
    params: dict[str, Any] = {"start": start, "max_cells": max_rows * max_cols}
    if user_id:
        where_filters.append("user_id = :user_id")
        params["user_id"] = user_id
    if app_id:
        where_filters.append("app_id = :app_id")
        params["app_id"] = app_id
    where_sql = " AND ".join(where_filters)

    # Top rows by total value (so the pivot shows the most relevant
    # rows when over the cap).
    top_rows_sql = f"""
        SELECT {row_expr} AS r, {val_expr} AS v
        FROM gateway_usage_events
        WHERE {where_sql}
        GROUP BY r
        ORDER BY v DESC
        LIMIT :max_rows
    """
    top_rows = (await db.execute(
        text(top_rows_sql), {**params, "max_rows": max_rows},
    )).all()
    row_keys = [str(r[0]) for r in top_rows]
    row_totals = {str(r[0]): float(r[1] or 0) for r in top_rows}
    if not row_keys:
        return {
            "row": row, "col": col, "value": value, "days": days,
            "row_keys": [], "col_keys": [], "cells": [],
            "row_totals": {}, "col_totals": {}, "grand_total": 0,
            "truncated": False,
        }

    top_cols_sql = f"""
        SELECT {col_expr} AS c, {val_expr} AS v
        FROM gateway_usage_events
        WHERE {where_sql}
        GROUP BY c
        ORDER BY v DESC
        LIMIT :max_cols
    """
    top_cols = (await db.execute(
        text(top_cols_sql), {**params, "max_cols": max_cols},
    )).all()
    col_keys = [str(c[0]) for c in top_cols]
    col_totals = {str(c[0]): float(c[1] or 0) for c in top_cols}

    # Pivot cells, restricted to the row/col keys we kept.
    pivot_sql = f"""
        SELECT {row_expr} AS r, {col_expr} AS c, {val_expr} AS v
        FROM gateway_usage_events
        WHERE {where_sql}
          AND {row_expr} = ANY(:row_keys)
          AND {col_expr} = ANY(:col_keys)
        GROUP BY r, c
    """
    pivot_rows = (await db.execute(
        text(pivot_sql),
        {**params, "row_keys": row_keys, "col_keys": col_keys},
    )).all()

    cells: list[dict[str, Any]] = [
        {"row": str(r[0]), "col": str(r[1]), "value": float(r[2] or 0)}
        for r in pivot_rows
    ]

    grand_total = sum(c["value"] for c in cells)

    # Was the result truncated by the caps? Yes if at least one of
    # rows/cols hit the limit AND there's likely more than that.
    truncated = (
        len(row_keys) == max_rows or len(col_keys) == max_cols
    )

    return {
        "row": row,
        "col": col,
        "value": value,
        "days": days,
        "row_keys": row_keys,
        "col_keys": col_keys,
        "row_totals": row_totals,
        "col_totals": col_totals,
        "cells": cells,
        "grand_total": round(grand_total, 6),
        "truncated": truncated,
        "available_dims": sorted(_PIVOT_DIMS.keys()),
        "available_values": sorted(_PIVOT_VALUES.keys()),
    }


@router.get("/v1/usage/me/sessions")
async def me_usage_sessions(
    days: int = 7,
    limit: int = 50,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Caller's recent sessions, most recent first."""
    if days < 1 or days > 90:
        raise HTTPException(400, detail="days must be 1-90")
    if limit < 1 or limit > 200:
        raise HTTPException(400, detail="limit must be 1-200")

    start = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        text("""
            SELECT
                external_sid,
                MAX(created_at) AS last_seen_at,
                MIN(created_at) AS first_seen_at,
                COUNT(*) AS events,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
                COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                MAX(app_id) AS app_id,
                COUNT(DISTINCT agent_id) FILTER (WHERE agent_id IS NOT NULL) AS distinct_agents
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND user_id = :uid
              AND external_sid IS NOT NULL
            GROUP BY external_sid
            ORDER BY last_seen_at DESC
            LIMIT :limit
        """),
        {"start": start, "uid": principal.user_id, "limit": limit},
    )).mappings().all()
    return {
        "days": days,
        "user_id": principal.user_id,
        "sessions": [
            {
                "session_id": r["external_sid"],
                "app_id": r["app_id"],
                "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
                "first_seen_at": r["first_seen_at"].isoformat() if r["first_seen_at"] else None,
                "events": int(r["events"]),
                "tokens": int(r["tokens"]),
                "cost_usd": round(float(r["cost_usd"] or 0), 6),
                "distinct_agents": int(r["distinct_agents"] or 0),
            }
            for r in rows
        ],
    }


# ── Audit timeline ────────────────────────────────────────────────
#
# Unified audit feed across all the append-only / mutation-tracked
# surfaces the gateway owns. Powers the dashboard's /audit page.
#
# Sources (in priority order):
#   1. gateway_user_plan_history — rich append-only plan change log
#      (kind: plan_change). Carries from/to, snapshot, reason, who.
#   2. gateway_models.updated_at — model catalog mutations
#      (kind: model_modified). Last-modified only; we don't keep an
#      append-only model_history table.
#   3. gateway_providers.updated_at — provider mutations
#      (kind: provider_modified). Same caveat.
#
# Each source is its own bounded query (small per-source LIMIT) and
# the results are merged + sorted in memory. Won't impact the
# dispatch hot path: this is a one-shot read against tables that are
# not touched by the request loop.


@router.get("/admin/audit/timeline")
async def admin_audit_timeline(
    days: int = 30,
    limit: int = 200,
    user_id: str | None = None,
    kinds: str | None = None,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Unified audit timeline.

    Returns events from every audit-tracked surface the gateway owns,
    merged and sorted most-recent-first.

    Query params:
        days    : window length, 1..365
        limit   : total events to return, 1..1000
        user_id : optional filter (applies to plan_change only)
        kinds   : optional comma-separated allowlist
                  (plan_change | model_modified | provider_modified)
    """
    _require_admin(principal)
    if days < 1 or days > 365:
        raise HTTPException(400, detail="days must be between 1 and 365")
    if limit < 1 or limit > 1000:
        raise HTTPException(400, detail="limit must be between 1 and 1000")

    allowed = (
        {k.strip() for k in kinds.split(",") if k.strip()}
        if kinds
        else None
    )
    start = datetime.now(timezone.utc) - timedelta(days=days)
    per_source_limit = max(1, limit)

    events: list[dict[str, Any]] = []

    # 1. Plan changes (rich append-only history).
    if not allowed or "plan_change" in allowed:
        params: dict[str, Any] = {"start": start, "limit": per_source_limit}
        user_clause = ""
        if user_id:
            params["uid"] = user_id
            user_clause = " AND user_id = :uid"
        plan_rows = (await db.execute(
            text(f"""
                SELECT
                    id, user_id, from_plan_id, to_plan_id,
                    changed_by, change_kind, reason, changed_at
                FROM gateway_user_plan_history
                WHERE changed_at >= :start
                {user_clause}
                ORDER BY changed_at DESC
                LIMIT :limit
            """),
            params,
        )).mappings().all()
        for r in plan_rows:
            events.append({
                "ts": r["changed_at"].isoformat() if r["changed_at"] else None,
                "kind": "plan_change",
                "subkind": r["change_kind"],
                "actor_id": r["changed_by"],
                "user_id": r["user_id"],
                "summary": _summarize_plan_change(
                    r["change_kind"], r["from_plan_id"], r["to_plan_id"],
                ),
                "details": {
                    "from_plan_id": r["from_plan_id"],
                    "to_plan_id": r["to_plan_id"],
                    "reason": r["reason"],
                },
            })

    # 2. Model catalog mutations (last-modified signal).
    if not allowed or "model_modified" in allowed:
        if not user_id:  # not applicable to model edits
            model_rows = (await db.execute(
                text("""
                    SELECT
                        alias, provider_slug, real_model_id,
                        updated_at, created_at
                    FROM gateway_models
                    WHERE updated_at >= :start
                    ORDER BY updated_at DESC
                    LIMIT :limit
                """),
                {"start": start, "limit": per_source_limit},
            )).mappings().all()
            for r in model_rows:
                is_create = (
                    r["created_at"] is not None
                    and r["updated_at"] is not None
                    and abs((r["updated_at"] - r["created_at"]).total_seconds()) < 1.0
                )
                events.append({
                    "ts": r["updated_at"].isoformat() if r["updated_at"] else None,
                    "kind": "model_modified",
                    "subkind": "created" if is_create else "updated",
                    "actor_id": None,
                    "user_id": None,
                    "summary": (
                        f"Model `{r['alias']}` "
                        f"{'created' if is_create else 'updated'} "
                        f"({r['provider_slug']} / {r['real_model_id']})"
                    ),
                    "details": {
                        "alias": r["alias"],
                        "provider_slug": r["provider_slug"],
                        "real_model_id": r["real_model_id"],
                    },
                })

    # 3. Provider mutations (last-modified signal).
    if not allowed or "provider_modified" in allowed:
        if not user_id:
            try:
                prov_rows = (await db.execute(
                    text("""
                        SELECT slug, name, updated_at, created_at
                        FROM gateway_providers
                        WHERE updated_at >= :start
                        ORDER BY updated_at DESC
                        LIMIT :limit
                    """),
                    {"start": start, "limit": per_source_limit},
                )).mappings().all()
            except Exception:
                # Older deployments without gateway_providers.updated_at:
                # silently skip rather than 500. We're optional info.
                prov_rows = []
            for r in prov_rows:
                is_create = (
                    r["created_at"] is not None
                    and r["updated_at"] is not None
                    and abs((r["updated_at"] - r["created_at"]).total_seconds()) < 1.0
                )
                events.append({
                    "ts": r["updated_at"].isoformat() if r["updated_at"] else None,
                    "kind": "provider_modified",
                    "subkind": "created" if is_create else "updated",
                    "actor_id": None,
                    "user_id": None,
                    "summary": (
                        f"Provider `{r['slug']}` "
                        f"{'created' if is_create else 'updated'}"
                        f"{' (' + r['name'] + ')' if r['name'] else ''}"
                    ),
                    "details": {"slug": r["slug"], "name": r["name"]},
                })

    # Merge + truncate.
    events.sort(key=lambda e: e.get("ts") or "", reverse=True)
    events = events[:limit]

    return {
        "days": days,
        "limit": limit,
        "user_id": user_id,
        "kinds": sorted(allowed) if allowed else None,
        "events": events,
        "sources": {
            "plan_change": "gateway_user_plan_history (full append-only history)",
            "model_modified": "gateway_models.updated_at (last-modified only)",
            "provider_modified": "gateway_providers.updated_at (last-modified only)",
        },
    }


def _summarize_plan_change(
    kind: str | None, from_plan: str | None, to_plan: str | None,
) -> str:
    if kind == "initial":
        return f"Initial plan: {to_plan or '?'}"
    if kind == "upgrade":
        return f"Upgrade: {from_plan or '?'} → {to_plan or '?'}"
    if kind == "downgrade":
        return f"Downgrade: {from_plan or '?'} → {to_plan or '?'}"
    if kind == "cancel":
        return f"Cancelled: {from_plan or '?'}"
    if kind == "reactivate":
        return f"Reactivated: {to_plan or '?'}"
    return f"Plan change: {from_plan or '?'} → {to_plan or '?'}"


# ── Re-cost compare matrix ────────────────────────────────────────
#
# "If my last 30 days of openai traffic had been routed through
# deepseek-chat / groq-llama / claude-haiku, what would each have
# cost me?" Answer: one aggregation against ``gateway_usage_events``
# filtered by the source alias, then a per-candidate re-cost using
# the candidate alias's prices from ``gateway_models``.
#
# Cost path is identical to the live dispatcher (4-tier cache pricing
# + per-minute audio), so the numbers are directly comparable to what
# the dashboard currently shows for the source.
#
# Performance: ONE aggregation query (covered by the partial index on
# (model, created_at) from migration 0011) + a single SELECT against
# ``gateway_models`` to fetch candidate prices. Hot path is never
# touched. Max window 365d, max candidates capped at 12.


@router.get("/admin/usage/compare-aliases")
async def admin_usage_compare_aliases(
    source: str,
    candidates: str,
    days: int = 30,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Re-cost the source alias's historical traffic under each
    candidate alias's prices.

    Query params:
        source     : the alias to pull volume from (e.g. ``gpt-4o``)
        candidates : comma-separated list of aliases to re-cost against
                     (max 12)
        days       : window length, 1..365
    """
    _require_admin(principal)
    if days < 1 or days > 365:
        raise HTTPException(400, detail="days must be between 1 and 365")
    cand_list = [c.strip() for c in candidates.split(",") if c.strip()]
    if not cand_list:
        raise HTTPException(400, detail="at least one candidate required")
    if len(cand_list) > 12:
        raise HTTPException(400, detail="max 12 candidates per call")
    if not source.strip():
        raise HTTPException(400, detail="source alias required")

    start = datetime.now(timezone.utc) - timedelta(days=days)

    # Aggregate the source's traffic, split chat vs transcription.
    agg = (await db.execute(
        text("""
            SELECT
                COUNT(*) AS requests,
                COUNT(*) FILTER (WHERE kind = 'transcription') AS transcriptions,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(audio_seconds), 0) AS audio_seconds,
                COALESCE(SUM(total_cost_usd), 0) AS actual_cost_usd
            FROM gateway_usage_events
            WHERE created_at >= :start
              AND model = :source
        """),
        {"start": start, "source": source},
    )).mappings().one()

    pin = int(agg["prompt_tokens"])
    pout = int(agg["completion_tokens"])
    cr = int(agg["cache_read_tokens"])
    cw = int(agg["cache_write_tokens"])
    secs = float(agg["audio_seconds"] or 0)
    actual = round(float(agg["actual_cost_usd"] or 0), 6)
    requests = int(agg["requests"])
    transcriptions = int(agg["transcriptions"] or 0)

    # Non-cached input tokens: the share we actually paid full price for.
    non_cached_input = max(0, pin - cr)
    minutes = secs / 60.0

    # Pull candidate (and source) prices in ONE query.
    aliases_to_lookup = list(set(cand_list + [source]))
    price_rows = (await db.execute(
        text("""
            SELECT
                alias, provider_slug,
                cost_per_1k_input_tokens AS p_in,
                cost_per_1k_output_tokens AS p_out,
                cost_per_1k_cache_read_tokens AS p_cr,
                cost_per_1k_cache_write_tokens AS p_cw,
                cost_per_minute_audio AS p_audio
            FROM gateway_models
            WHERE alias = ANY(:aliases)
        """),
        {"aliases": aliases_to_lookup},
    )).mappings().all()

    prices: dict[str, dict[str, Any]] = {r["alias"]: dict(r) for r in price_rows}

    def _recost(alias: str) -> dict[str, Any]:
        row = prices.get(alias)
        if not row:
            return {
                "alias": alias,
                "provider": None,
                "found": False,
                "cost_usd": None,
                "delta_usd": None,
                "delta_pct": None,
            }
        # 4-tier cache pricing — identical formula to the live
        # dispatcher's ``compute_cost`` in cost_path.
        chat_cost = (
            (non_cached_input / 1000.0) * float(row["p_in"] or 0)
            + (cr / 1000.0) * float(row["p_cr"] or 0)
            + (cw / 1000.0) * float(row["p_cw"] or 0)
            + (pout / 1000.0) * float(row["p_out"] or 0)
        )
        audio_cost = minutes * float(row["p_audio"] or 0)
        total = round(chat_cost + audio_cost, 6)
        delta = round(total - actual, 6)
        delta_pct = (delta / actual * 100.0) if actual > 0 else None
        return {
            "alias": alias,
            "provider": row["provider_slug"],
            "found": True,
            "cost_usd": total,
            "chat_cost_usd": round(chat_cost, 6),
            "audio_cost_usd": round(audio_cost, 6),
            "delta_usd": delta,
            "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        }

    candidate_results = [_recost(c) for c in cand_list]

    return {
        "source": source,
        "days": days,
        "volume": {
            "requests": requests,
            "transcriptions": transcriptions,
            "prompt_tokens": pin,
            "completion_tokens": pout,
            "non_cached_input_tokens": non_cached_input,
            "cache_read_tokens": cr,
            "cache_write_tokens": cw,
            "audio_seconds": round(secs, 2),
            "audio_minutes": round(minutes, 4),
        },
        "actual_cost_usd": actual,
        "candidates": candidate_results,
    }
