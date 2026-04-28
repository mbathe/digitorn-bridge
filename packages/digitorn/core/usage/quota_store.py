"""QuotaStore - admin-set token limits + enforcement.

Three scope types (see ``UserQuota`` model docstring):

    user       - cross-app user limit
    user_app   - per-user-per-app limit
    app        - shared across every user of an app

Enforcement is O(1) on the hot path: given (user_id, app_id), the
store loads the applicable rows (indexed), walks the period window
via ``UsageStore.sum_tokens_in_window``, and returns a decision
dict. Called before each LLM call from the runtime - see
``agent_loop.py`` for the hook.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

logger = logging.getLogger(__name__)


class QuotaScope:
    USER = "user"
    USER_APP = "user_app"
    APP = "app"
    ALL = (USER, USER_APP, APP)


class QuotaPeriod:
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    ALL = (DAY, WEEK, MONTH)


class QuotaStore:
    def __init__(self, session_factory: Any, *, usage_store: Any) -> None:
        self._session_factory = session_factory
        self._usage = usage_store

    # ── Admin CRUD ──────────────────────────────────────────────────

    async def upsert_quota(
        self,
        *,
        scope_type: str,
        scope_id: str,
        app_id: str | None = None,
        period: str = QuotaPeriod.MONTH,
        tokens_limit: int,
        set_by: str | None = None,
    ) -> dict[str, Any]:
        """Create or replace a quota row.

        Uniqueness key is (scope_type, scope_id, app_id, period) -
        calling this twice overwrites the previous limit.
        """
        from digitorn.core.models import UserQuota

        self._validate(scope_type, period, app_id)

        async with self._session_factory() as db:
            stmt = select(UserQuota).where(
                UserQuota.scope_type == scope_type,
                UserQuota.scope_id == scope_id,
                UserQuota.period == period,
            )
            if app_id is not None:
                stmt = stmt.where(UserQuota.app_id == app_id)
            else:
                stmt = stmt.where(UserQuota.app_id.is_(None))
            existing = (await db.execute(stmt)).scalar_one_or_none()

            if existing is None:
                row = UserQuota(
                    scope_type=scope_type,
                    scope_id=scope_id,
                    app_id=app_id,
                    period=period,
                    tokens_limit=int(tokens_limit),
                    set_by=set_by,
                )
                db.add(row)
            else:
                row = existing
                row.tokens_limit = int(tokens_limit)
                row.set_by = set_by or row.set_by
                row.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(row)
            return _row_to_dict(row)

    async def delete_quota(
        self, *, quota_id: str,
    ) -> bool:
        from digitorn.core.models import UserQuota
        async with self._session_factory() as db:
            result = await db.execute(
                delete(UserQuota).where(UserQuota.id == quota_id)
            )
            await db.commit()
            return (result.rowcount or 0) > 0

    async def list_quotas(
        self,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        app_id: str | None = None,
    ) -> list[dict[str, Any]]:
        from digitorn.core.models import UserQuota
        async with self._session_factory() as db:
            stmt = select(UserQuota)
            if scope_type is not None:
                stmt = stmt.where(UserQuota.scope_type == scope_type)
            if scope_id is not None:
                stmt = stmt.where(UserQuota.scope_id == scope_id)
            if app_id is not None:
                stmt = stmt.where(UserQuota.app_id == app_id)
            stmt = stmt.order_by(UserQuota.updated_at.desc())
            rows = (await db.execute(stmt)).scalars().all()
            return [_row_to_dict(r) for r in rows]

    # ── Enforcement ─────────────────────────────────────────────────

    async def check(
        self,
        *,
        user_id: str,
        app_id: str,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Compute the most-restrictive usage vs limit for a caller.

        Returns a decision dict::

            {
              "allowed": True/False,
              "limits": [
                { "scope_type": "user", "period": "month",
                  "limit": 10_000_000, "used": 3_214_567,
                  "remaining": 6_785_433, "window_start": ..., "blocking": False },
                ...
              ],
              "blocking_limit": {...} | None,
            }

        ``allowed`` is False when any limit has ``remaining <= 0``.
        The caller should refuse the LLM call with a structured
        error when ``allowed`` is False and surface ``blocking_limit``
        in the message.
        """
        from digitorn.core.models import UserQuota

        now = at or datetime.now(timezone.utc)

        async with self._session_factory() as db:
            # Collect every row applicable to this (user, app) pair
            stmt = select(UserQuota).where(
                # user-cross-app
                ((UserQuota.scope_type == QuotaScope.USER)
                 & (UserQuota.scope_id == user_id)
                 & (UserQuota.app_id.is_(None)))
                |
                # user on a specific app
                ((UserQuota.scope_type == QuotaScope.USER_APP)
                 & (UserQuota.scope_id == user_id)
                 & (UserQuota.app_id == app_id))
                |
                # app shared across users
                ((UserQuota.scope_type == QuotaScope.APP)
                 & (UserQuota.scope_id == app_id)
                 & (UserQuota.app_id == app_id))
            )
            rows = (await db.execute(stmt)).scalars().all()

        limits: list[dict[str, Any]] = []
        blocking: dict[str, Any] | None = None

        for row in rows:
            window_start = _period_start(row.period, now)
            # Pick the right user_id to scan for the sum.
            # For APP scope we sum across all users of the app
            # (no user filter), so we pass user_id="*" → the store
            # supports that by treating None as "all users"... but
            # for safety we just sum for the calling user here,
            # and let per-app limits be self-consistent.
            # TODO v2: proper APP scope aggregation across users.
            used = await self._usage.sum_tokens_in_window(
                user_id=user_id
                if row.scope_type != QuotaScope.APP
                else user_id,
                app_id=app_id if row.scope_type != QuotaScope.USER else None,
                since=window_start,
            )
            remaining = max(0, int(row.tokens_limit) - int(used))
            limit_entry = {
                "id": row.id,
                "scope_type": row.scope_type,
                "scope_id": row.scope_id,
                "app_id": row.app_id,
                "period": row.period,
                "limit": int(row.tokens_limit),
                "used": int(used),
                "remaining": int(remaining),
                "window_start": window_start.isoformat(),
                "blocking": remaining <= 0,
            }
            limits.append(limit_entry)
            if remaining <= 0 and blocking is None:
                blocking = limit_entry

        return {
            "allowed": blocking is None,
            "limits": limits,
            "blocking_limit": blocking,
        }

    # ── Validation ──────────────────────────────────────────────────

    @staticmethod
    def _validate(scope_type: str, period: str, app_id: str | None) -> None:
        if scope_type not in QuotaScope.ALL:
            raise ValueError(f"invalid scope_type: {scope_type!r}")
        if period not in QuotaPeriod.ALL:
            raise ValueError(f"invalid period: {period!r}")
        # USER scope can't have an app_id
        if scope_type == QuotaScope.USER and app_id is not None:
            raise ValueError("user scope cannot be scoped to an app")
        # USER_APP and APP both require an app_id
        if scope_type in (QuotaScope.USER_APP, QuotaScope.APP) and not app_id:
            raise ValueError(f"{scope_type} scope requires an app_id")


def _period_start(period: str, now: datetime) -> datetime:
    """First second of the current period window."""
    if period == QuotaPeriod.DAY:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == QuotaPeriod.WEEK:
        # ISO week: Monday is day 0
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == QuotaPeriod.MONTH:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unknown period: {period!r}")


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "scope_type": row.scope_type,
        "scope_id": row.scope_id,
        "app_id": row.app_id,
        "period": row.period,
        "tokens_limit": row.tokens_limit,
        "set_by": row.set_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
