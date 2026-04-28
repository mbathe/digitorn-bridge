"""UsageStore - append-only writer + aggregation queries for usage_events.

The store is called from two places:

1. **AppManager** at the end of each turn. One ``record()`` call
   per (user, app, session, model) with the tokens returned by the
   LLM provider. Cost is computed on the fly via ``compute_cost``.

2. **UserUsageSummary** route (``GET /api/users/me/usage``). Reads
   the same table using grouped aggregation queries.

The aggregations return plain dicts - no ORM rows leak into the
API layer. This keeps the serialization path trivial.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, select

from digitorn.core.usage.price_book import compute_cost

logger = logging.getLogger(__name__)


class UsageStore:
    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    # ── Write ───────────────────────────────────────────────────────

    async def record(
        self,
        *,
        user_id: str,
        app_id: str,
        session_id: str | None,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        credential_id: str | None = None,
        cost_usd: float | None = None,
    ) -> dict[str, Any]:
        """Append one usage event. Computes cost if not supplied."""
        from digitorn.core.models import UsageEvent

        if cost_usd is None:
            cost_usd = compute_cost(model, prompt_tokens, completion_tokens)

        async with self._session_factory() as db:
            row = UsageEvent(
                user_id=user_id,
                app_id=app_id,
                session_id=session_id,
                provider=provider,
                model=model,
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
                cost_usd=float(cost_usd),
                credential_id=credential_id,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return _row_to_dict(row)

    # ── Aggregations for GET /api/users/me/usage ────────────────────

    async def monthly_totals(
        self,
        *,
        user_id: str,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Return tokens + cost for the current calendar month."""
        from digitorn.core.models import UsageEvent

        now = at or datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

        async with self._session_factory() as db:
            stmt = (
                select(
                    func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
                    func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
                )
                .where(
                    UsageEvent.user_id == user_id,
                    UsageEvent.created_at >= month_start,
                )
            )
            pt, ct, cost = (await db.execute(stmt)).one()
        return {
            "prompt_tokens": int(pt or 0),
            "completion_tokens": int(ct or 0),
            "total_tokens": int(pt or 0) + int(ct or 0),
            "cost_usd": round(float(cost or 0.0), 6),
            "period_start": month_start.isoformat(),
        }

    async def cost_by_model(
        self,
        *,
        user_id: str,
        since: datetime,
    ) -> dict[str, float]:
        """USD cost grouped by model, since ``since`` (inclusive)."""
        from digitorn.core.models import UsageEvent
        async with self._session_factory() as db:
            stmt = (
                select(
                    UsageEvent.model,
                    func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
                )
                .where(
                    UsageEvent.user_id == user_id,
                    UsageEvent.created_at >= since,
                )
                .group_by(UsageEvent.model)
            )
            rows = (await db.execute(stmt)).all()
        return {
            (model or "unknown"): round(float(cost or 0.0), 6)
            for model, cost in rows
        }

    async def totals_by_session(
        self,
        *,
        app_id: str,
        session_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Sum tokens + cost grouped by session_id for a given app.

        Used by the session-list endpoint to hydrate each row with
        accurate token/cost totals from the persisted usage_events.
        Returns a dict keyed by session_id - sessions with no events
        are absent (caller zero-fills).
        """
        from digitorn.core.models import UsageEvent
        if not session_ids:
            return {}
        async with self._session_factory() as db:
            stmt = (
                select(
                    UsageEvent.session_id,
                    func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
                    func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
                )
                .where(
                    UsageEvent.app_id == app_id,
                    UsageEvent.session_id.in_(session_ids),
                )
                .group_by(UsageEvent.session_id)
            )
            rows = (await db.execute(stmt)).all()
        out: dict[str, dict[str, Any]] = {}
        for sid, pt, ct, cost in rows:
            if not sid:
                continue
            pt = int(pt or 0)
            ct = int(ct or 0)
            out[sid] = {
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "tokens": pt + ct,
                "cost_usd": round(float(cost or 0.0), 6),
            }
        return out

    async def by_app(
        self,
        *,
        user_id: str,
        since: datetime,
    ) -> list[dict[str, Any]]:
        """Tokens + cost grouped by app for the caller's usage."""
        from digitorn.core.models import UsageEvent
        async with self._session_factory() as db:
            stmt = (
                select(
                    UsageEvent.app_id,
                    func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
                    func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
                )
                .where(
                    UsageEvent.user_id == user_id,
                    UsageEvent.created_at >= since,
                )
                .group_by(UsageEvent.app_id)
                .order_by(func.sum(UsageEvent.cost_usd).desc())
            )
            rows = (await db.execute(stmt)).all()
        out: list[dict[str, Any]] = []
        for app_id, pt, ct, cost in rows:
            out.append({
                "app_id": app_id,
                "prompt_tokens": int(pt or 0),
                "completion_tokens": int(ct or 0),
                "tokens": int(pt or 0) + int(ct or 0),
                "cost_usd": round(float(cost or 0.0), 6),
            })
        return out

    async def timeseries_hourly(
        self,
        *,
        user_id: str,
        hours: int = 24,
        at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return ``hours``-long hourly time series of tokens.

        One bucket per hour, zero-filled for hours with no activity
        so the client can render a continuous line chart without
        gaps.
        """
        from digitorn.core.models import UsageEvent

        now = (at or datetime.now(timezone.utc)).replace(
            minute=0, second=0, microsecond=0,
        )
        start = now - timedelta(hours=hours - 1)

        async with self._session_factory() as db:
            stmt = (
                select(
                    UsageEvent.created_at,
                    UsageEvent.prompt_tokens,
                    UsageEvent.completion_tokens,
                )
                .where(
                    UsageEvent.user_id == user_id,
                    UsageEvent.created_at >= start,
                )
            )
            raw_rows = (await db.execute(stmt)).all()

        buckets: dict[str, dict[str, int]] = defaultdict(
            lambda: {"prompt": 0, "completion": 0}
        )
        for ts, pt, ct in raw_rows:
            key = _hour_bucket(ts).isoformat()
            buckets[key]["prompt"] += int(pt or 0)
            buckets[key]["completion"] += int(ct or 0)

        out: list[dict[str, Any]] = []
        for i in range(hours):
            ts = start + timedelta(hours=i)
            key = ts.isoformat()
            data = buckets.get(key, {"prompt": 0, "completion": 0})
            out.append({
                "ts": key,
                "prompt": data["prompt"],
                "completion": data["completion"],
            })
        return out

    async def timeseries_daily(
        self,
        *,
        user_id: str,
        days: int = 30,
        at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return ``days``-long daily time series of tokens.

        Same pattern as hourly - zero-filled for continuous chart.
        """
        from digitorn.core.models import UsageEvent

        now = (at or datetime.now(timezone.utc)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        start = now - timedelta(days=days - 1)

        async with self._session_factory() as db:
            stmt = (
                select(
                    UsageEvent.created_at,
                    UsageEvent.prompt_tokens,
                    UsageEvent.completion_tokens,
                )
                .where(
                    UsageEvent.user_id == user_id,
                    UsageEvent.created_at >= start,
                )
            )
            raw_rows = (await db.execute(stmt)).all()

        buckets: dict[str, dict[str, int]] = defaultdict(
            lambda: {"prompt": 0, "completion": 0}
        )
        for ts, pt, ct in raw_rows:
            key = _day_bucket(ts).strftime("%Y-%m-%d")
            buckets[key]["prompt"] += int(pt or 0)
            buckets[key]["completion"] += int(ct or 0)

        out: list[dict[str, Any]] = []
        for i in range(days):
            ts = start + timedelta(days=i)
            key = ts.strftime("%Y-%m-%d")
            data = buckets.get(key, {"prompt": 0, "completion": 0})
            out.append({
                "date": key,
                "prompt": data["prompt"],
                "completion": data["completion"],
            })
        return out

    # ── Used by QuotaStore to check rolling-window totals ───────────

    async def sum_tokens_in_window(
        self,
        *,
        user_id: str,
        app_id: str | None,
        since: datetime,
    ) -> int:
        """Total tokens (prompt + completion) for a user, optionally
        narrowed to one app, from ``since`` to now. Used by the
        quota enforcement check."""
        from digitorn.core.models import UsageEvent
        async with self._session_factory() as db:
            stmt = (
                select(
                    func.coalesce(func.sum(UsageEvent.prompt_tokens), 0)
                    + func.coalesce(func.sum(UsageEvent.completion_tokens), 0)
                )
                .where(
                    UsageEvent.user_id == user_id,
                    UsageEvent.created_at >= since,
                )
            )
            if app_id is not None:
                stmt = stmt.where(UsageEvent.app_id == app_id)
            total = (await db.execute(stmt)).scalar() or 0
            return int(total)

    async def credential_usage(
        self,
        *,
        credential_id: str,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        """Aggregate usage for a specific credential. Powers the
        per-credential stats shown in the Credentials settings UI.
        """
        from digitorn.core.models import UsageEvent
        async with self._session_factory() as db:
            stmt = select(
                func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
                func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
                func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
                func.max(UsageEvent.created_at),
            ).where(UsageEvent.credential_id == credential_id)
            if since is not None:
                stmt = stmt.where(UsageEvent.created_at >= since)
            pt, ct, cost, last_used = (await db.execute(stmt)).one()
        return {
            "prompt_tokens": int(pt or 0),
            "completion_tokens": int(ct or 0),
            "total_tokens": int(pt or 0) + int(ct or 0),
            "cost_usd": round(float(cost or 0.0), 6),
            "last_used_at": last_used.isoformat() if last_used else None,
        }


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "app_id": row.app_id,
        "session_id": row.session_id,
        "provider": row.provider,
        "model": row.model,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "cost_usd": row.cost_usd,
        "credential_id": row.credential_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _hour_bucket(ts: datetime) -> datetime:
    """Normalize to the top of the hour in UTC.

    SQLite returns tz-naive datetimes for ``DateTime(timezone=True)``
    columns, so we force a UTC tzinfo to keep the isoformat key
    consistent with the zero-filled series buckets built in the
    caller (which are always tz-aware).
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0,
    )


def _day_bucket(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
