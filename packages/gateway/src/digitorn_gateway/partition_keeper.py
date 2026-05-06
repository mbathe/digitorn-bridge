"""Daily background task: keep partition tables one quarter ahead.

The Postgres migration created 13 monthly partitions for
``gateway_usage_events`` (current + 12). This task wakes daily,
checks the most distant partition, and creates more if we're
within 60 days of the edge. The partitioned parent table refuses
inserts that don't fit a child, so falling behind here would 5xx
every LLM call.

We rely on the helper function created by sprint E:
``gateway_create_usage_partition(target_month)``.

The task is started by the gateway lifespan and cancelled at
shutdown. Failures are logged + swallowed; the next tick retries.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from digitorn_gateway.db import get_session_factory

logger = logging.getLogger(__name__)


# Stay this many days ahead of "now" - if the most distant
# partition is closer than this we create more.
HEADROOM_DAYS = 60

# Sleep between checks. Daily is plenty - the only thing it
# guards against is the partition tail catching up to NOW.
TICK_INTERVAL_SECONDS = 24 * 3600


async def run() -> None:
    """Run the daily partition keeper. Cancellable; never raises."""
    while True:
        try:
            await _ensure_partitions_ahead()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("partition_keeper_tick_failed: %s", exc, exc_info=True)
        try:
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def _ensure_partitions_ahead() -> None:
    factory = get_session_factory()
    async with factory() as db:
        # Find the latest existing partition's upper bound.
        latest = (await db.execute(
            text("""
                SELECT MAX(
                    (regexp_match(relname, '_(\\d{4})_(\\d{2})$'))[1] || '-' ||
                    (regexp_match(relname, '_(\\d{4})_(\\d{2})$'))[2]
                )
                FROM pg_class
                WHERE relname LIKE 'gateway_usage_events_____\\___'
                  AND relkind = 'r'
                  AND relnamespace = 'public'::regnamespace
            """)
        )).scalar()

        # Frontier date as midnight on the first of the discovered month;
        # null when no partitions exist yet (call seed for the next 13).
        if latest:
            year, month = latest.split("-")
            frontier = datetime(int(year), int(month), 1, tzinfo=timezone.utc)
        else:
            frontier = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0,
            )

        target = datetime.now(timezone.utc) + timedelta(days=HEADROOM_DAYS)

        # Walk monthly until we're past the target.
        cursor = frontier
        created = 0
        while cursor <= target:
            await db.execute(
                text(
                    "SELECT gateway_create_usage_partition(:m)"
                ),
                {"m": cursor.date()},
            )
            # advance one month
            year = cursor.year + (1 if cursor.month == 12 else 0)
            month = 1 if cursor.month == 12 else cursor.month + 1
            cursor = datetime(year, month, 1, tzinfo=timezone.utc)
            created += 1

        await db.commit()
        if created > 0:
            logger.info(
                "partition_keeper: created %d partitions up to %s",
                created, target.date(),
            )
