"""Activation Store — database-backed record of background trigger fires.

Each time a background trigger activates the agent, an Activation row
is created, updated during execution, and persisted to the database.

All operations are async (SQLAlchemy async sessions).

Usage::

    store = ActivationStore(session_factory)

    # Create
    activation_id = await store.create(app_id, trigger_id, "cron", message)

    # Complete
    await store.complete(activation_id, response="...", tool_calls_count=3, ...)

    # Query
    activations = await store.list(app_id, limit=20)
    activation = await store.get(activation_id)
    stats = await store.stats(app_id)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update

logger = logging.getLogger(__name__)


class ActivationStore:
    """Database-backed activation store using SQLAlchemy async sessions."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        app_id: str,
        trigger_id: str,
        trigger_type: str,
        message: str = "",
        trigger_payload: dict | None = None,
    ) -> str:
        """Create a new activation record. Returns the activation ID."""
        from digitorn.core.models import Activation

        activation = Activation(
            app_id=app_id,
            trigger_id=trigger_id,
            trigger_type=trigger_type,
            message=message[:10000],
            trigger_payload=trigger_payload or {},
            status="running",
        )

        async with self._session_factory() as session:
            session.add(activation)
            await session.commit()
            return activation.id

    async def complete(
        self,
        activation_id: str,
        *,
        response: str = "",
        tool_calls_count: int = 0,
        turns_used: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: str | None = None,
    ) -> None:
        """Mark an activation as completed or failed."""
        from digitorn.core.models import Activation

        now = datetime.now(timezone.utc)

        async with self._session_factory() as session:
            stmt = (
                update(Activation)
                .where(Activation.id == activation_id)
                .values(
                    status="failed" if error else "completed",
                    completed_at=now,
                    response=response[:50000],
                    tool_calls_count=tool_calls_count,
                    turns_used=turns_used,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    error=error,
                )
            )
            await session.execute(stmt)

            # Compute duration
            result = await session.execute(
                select(Activation.started_at).where(Activation.id == activation_id)
            )
            started = result.scalar_one_or_none()
            if started:
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                duration = (now - started).total_seconds() * 1000
                await session.execute(
                    update(Activation)
                    .where(Activation.id == activation_id)
                    .values(duration_ms=duration)
                )

            await session.commit()

    async def get(self, activation_id: str) -> dict[str, Any] | None:
        """Get a single activation by ID."""
        from digitorn.core.models import Activation

        async with self._session_factory() as session:
            result = await session.execute(
                select(Activation).where(Activation.id == activation_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_dict(row)

    async def list(
        self,
        app_id: str,
        limit: int = 20,
        offset: int = 0,
        trigger_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List activations for an app (newest first)."""
        from digitorn.core.models import Activation

        async with self._session_factory() as session:
            stmt = (
                select(Activation)
                .where(Activation.app_id == app_id)
                .order_by(Activation.started_at.desc())
            )
            if trigger_id:
                stmt = stmt.where(Activation.trigger_id == trigger_id)
            if status:
                stmt = stmt.where(Activation.status == status)
            stmt = stmt.offset(offset).limit(limit)

            result = await session.execute(stmt)
            return [self._row_to_dict(row) for row in result.scalars().all()]

    async def count(self, app_id: str) -> int:
        """Total activation count for an app."""
        from digitorn.core.models import Activation

        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count(Activation.id)).where(Activation.app_id == app_id)
            )
            return result.scalar() or 0

    async def stats(self, app_id: str) -> dict[str, Any]:
        """Aggregated statistics computed from the database."""
        from digitorn.core.models import Activation

        async with self._session_factory() as session:
            # Aggregates
            result = await session.execute(
                select(
                    func.count(Activation.id).label("total"),
                    func.sum(Activation.duration_ms).label("total_duration"),
                    func.avg(Activation.duration_ms).label("avg_duration"),
                    func.sum(Activation.prompt_tokens).label("total_prompt"),
                    func.sum(Activation.completion_tokens).label("total_completion"),
                    func.sum(Activation.tool_calls_count).label("total_tools"),
                    func.max(Activation.started_at).label("last_at"),
                ).where(Activation.app_id == app_id)
            )
            row = result.one()
            total = row.total or 0

            # Count completed/failed separately (reliable across all DB backends)
            completed = (await session.execute(
                select(func.count(Activation.id))
                .where(Activation.app_id == app_id, Activation.status == "completed")
            )).scalar() or 0

            failed = (await session.execute(
                select(func.count(Activation.id))
                .where(Activation.app_id == app_id, Activation.status == "failed")
            )).scalar() or 0

            return {
                "total": total,
                "completed": completed,
                "failed": failed,
                "total_duration_ms": round(float(row.total_duration or 0), 1),
                "avg_duration_ms": round(float(row.avg_duration or 0), 1),
                "total_prompt_tokens": int(row.total_prompt or 0),
                "total_completion_tokens": int(row.total_completion or 0),
                "total_tool_calls": int(row.total_tools or 0),
                "last_activation_at": str(row.last_at) if row.last_at else None,
                "success_rate": round(completed / max(total, 1) * 100, 1),
            }

    async def recent_errors(self, app_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get recent failed activations."""
        return await self.list(app_id, limit=limit, status="failed")

    async def record_event(
        self,
        *,
        activation_id: str,
        sequence: int,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Persist one timeline event for an activation.

        Cheap single-row INSERT. Failure is logged and swallowed — an
        event-store outage must NEVER break the live activation.
        """
        from digitorn.core.models import ActivationEvent

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    row = ActivationEvent(
                        activation_id=activation_id,
                        sequence=sequence,
                        event_type=event_type,
                        data=data,
                    )
                    session.add(row)
        except Exception as exc:
            # Don't raise — we don't want telemetry loss to abort an
            # otherwise successful activation.
            import logging
            logging.getLogger(__name__).warning(
                "record_event failed activation=%s type=%s: %s",
                activation_id, event_type, exc,
            )

    async def get_event(
        self, event_id: str, *, app_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Look up a single event by its id, joined to its activation.

        When ``app_id`` is supplied, the lookup also checks that the
        owning activation belongs to that app — returning ``None`` on a
        mismatch so a malicious caller can't use a known event_id of an
        app they don't control to pivot into another app's artifacts.
        """
        from digitorn.core.models import Activation, ActivationEvent

        async with self._session_factory() as session:
            stmt = (
                select(ActivationEvent, Activation.app_id)
                .join(Activation, ActivationEvent.activation_id == Activation.id)
                .where(ActivationEvent.id == event_id)
            )
            result = await session.execute(stmt)
            row = result.one_or_none()

        if row is None:
            return None
        event, row_app_id = row
        if app_id is not None and row_app_id != app_id:
            return None
        return {
            "id": event.id,
            "activation_id": event.activation_id,
            "sequence": event.sequence,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "event_type": event.event_type,
            "data": event.data or {},
            "app_id": row_app_id,
        }

    async def list_events(
        self, activation_id: str, *, event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all events for an activation, oldest first.

        Optional ``event_type`` filters to a single kind (e.g. only
        ``tool_call`` or only ``artifact``). The result is a list of
        plain dicts with ``{id, sequence, timestamp, event_type,
        data}`` — ready to serialise into the API response.
        """
        from digitorn.core.models import ActivationEvent

        async with self._session_factory() as session:
            stmt = (
                select(ActivationEvent)
                .where(ActivationEvent.activation_id == activation_id)
                .order_by(ActivationEvent.sequence)
            )
            if event_type:
                stmt = stmt.where(ActivationEvent.event_type == event_type)
            result = await session.execute(stmt)
            rows = result.scalars().all()

        return [
            {
                "id": r.id,
                "sequence": r.sequence,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "event_type": r.event_type,
                "data": r.data or {},
            }
            for r in rows
        ]

    async def count_by_status(self, app_id: str) -> dict[str, int]:
        """Return a {status: count} map for every status an app has seen.

        Cheap aggregate — one query backed by the ``ix_activations_app_status``
        composite index. Used by the dashboard to drive the live ``● running``
        indicator (status='running' > 0) and the summary counters.
        """
        from digitorn.core.models import Activation

        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    Activation.status, func.count(Activation.id),
                )
                .where(Activation.app_id == app_id)
                .group_by(Activation.status)
            )
            return {row[0]: int(row[1]) for row in result.all()}

    async def hourly_buckets(
        self, app_id: str, hours: int = 24,
    ) -> list[dict[str, Any]]:
        """Return a per-hour bucket list for the last N hours.

        Each bucket is ``{hour_utc: "YYYY-MM-DDTHH:00Z", total, completed,
        failed}``. Always returns exactly ``hours`` buckets in chronological
        order (oldest first), including empty ones, so the Flutter sparkline
        can render a fixed-width strip without client-side gap filling.

        Implementation note: we fetch every row in the window and bucket
        in Python rather than using DATE_TRUNC because the target is
        SQLite which does not have that function. For apps with < 10k
        activations per day this is fast; beyond that we can swap to a
        materialized view or dialect-specific SQL.
        """
        from datetime import datetime, timedelta, timezone

        from digitorn.core.models import Activation

        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        window_start = now - timedelta(hours=hours - 1)

        buckets: list[dict[str, Any]] = []
        by_hour: dict[datetime, dict[str, int]] = {}
        for i in range(hours):
            hour = window_start + timedelta(hours=i)
            by_hour[hour] = {"total": 0, "completed": 0, "failed": 0}

        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    Activation.started_at, Activation.status,
                )
                .where(
                    Activation.app_id == app_id,
                    Activation.started_at >= window_start,
                )
            )
            for started, status in result.all():
                if started is None:
                    continue
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                hour = started.astimezone(timezone.utc).replace(
                    minute=0, second=0, microsecond=0,
                )
                if hour not in by_hour:
                    continue  # outside the window (clock drift, racing insert)
                by_hour[hour]["total"] += 1
                if status == "completed":
                    by_hour[hour]["completed"] += 1
                elif status == "failed":
                    by_hour[hour]["failed"] += 1

        for i in range(hours):
            hour = window_start + timedelta(hours=i)
            b = by_hour[hour]
            buckets.append({
                "hour_utc": hour.isoformat().replace("+00:00", "Z"),
                "total": b["total"],
                "completed": b["completed"],
                "failed": b["failed"],
            })
        return buckets

    async def delete_for_app(self, app_id: str) -> int:
        """Delete all activations for an app.

        BUG-106: previously this ran silently, so ops would see the
        activation counter drop (e.g. 80 → 42) with no log line
        explaining the cause. We now emit an INFO-level audit line
        with the exact number of rows wiped and a traceback snippet
        so the call site is easy to identify.
        """
        from digitorn.core.models import Activation
        from sqlalchemy import delete
        import traceback as _tb

        async with self._session_factory() as session:
            result = await session.execute(
                delete(Activation).where(Activation.app_id == app_id)
            )
            await session.commit()
            rowcount = result.rowcount or 0
            if rowcount:
                stack = "".join(_tb.format_stack()[-5:-1]).replace("\n", " | ")
                logger.info(
                    "activation_store_wiped app=%s rows=%d caller=%s",
                    app_id, rowcount, stack,
                )
            return rowcount

    async def sweep_stuck_running(self, older_than_seconds: int = 600) -> int:
        """Mark activations pinned in ``status=running`` as failed.

        An activation row is created at the start of ``_run_single_activation``
        and completed at the end. If the daemon crashes between those
        two points — OOM, hard kill, power cut, pre-fix BUG-054 code
        path — the row stays ``running`` forever and pollutes the
        dashboard counters.

        This sweeper is idempotent: ``fail`` only touches rows whose
        ``started_at`` is older than ``older_than_seconds`` (default
        10 min — longer than any real turn), so a legitimately running
        activation is never stolen by the sweeper.

        Returns the number of rows marked failed.
        """
        from datetime import datetime, timedelta, timezone

        from digitorn.core.models import Activation

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        async with self._session_factory() as session:
            result = await session.execute(
                update(Activation)
                .where(
                    Activation.status == "running",
                    Activation.started_at < cutoff,
                )
                .values(
                    status="failed",
                    completed_at=datetime.now(timezone.utc),
                    error="zombie_activation_swept",
                )
            )
            await session.commit()
            return result.rowcount or 0

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "app_id": row.app_id,
            "trigger_id": row.trigger_id,
            "trigger_type": row.trigger_type,
            "status": row.status,
            "message": row.message,
            "trigger_payload": row.trigger_payload or {},
            "started_at": str(row.started_at) if row.started_at else None,
            "completed_at": str(row.completed_at) if row.completed_at else None,
            "duration_ms": round(row.duration_ms or 0, 1),
            "response": row.response or "",
            "tool_calls_count": row.tool_calls_count or 0,
            "turns_used": row.turns_used or 0,
            "error": row.error,
            "prompt_tokens": row.prompt_tokens or 0,
            "completion_tokens": row.completion_tokens or 0,
        }
