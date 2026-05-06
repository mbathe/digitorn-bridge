"""Daily background task: enforce per-action retention on history_log.

For each row in ``audit_actions_catalog`` whose ``retention_days``
has elapsed against the corresponding ``history_log.type`` rows, we
delete the row. The catalog has 18 seeded action_keys plus whatever
the operator has added; this task auto-discovers them all.

For ``credential_audit`` we apply the same per-action retention.

Implementation note: with monthly partitioning of ``history_log``
the most efficient cleanup is ``DROP TABLE history_log_YYYY_MM`` for
fully-aged partitions. Sprint G partitioning was deferred on the Neon
free tier, so we fall back to per-row DELETE for now. When the
partition swap completes, this task should switch to partition drop
(detect partitioned via ``pg_class.relkind='p'``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from digitorn_gateway.db import get_session_factory

logger = logging.getLogger(__name__)


TICK_INTERVAL_SECONDS = 24 * 3600


async def run() -> None:
    """Run the daily retention sweep. Cancellable; never raises."""
    # Stagger the start so multiple gateway replicas don't race.
    await asyncio.sleep(60)
    while True:
        try:
            await _sweep()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("retention_keeper_tick_failed: %s", exc, exc_info=True)
        try:
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def _sweep() -> None:
    factory = get_session_factory()
    async with factory() as db:
        catalog_rows = (await db.execute(
            text("SELECT action_key, retention_days FROM audit_actions_catalog")
        )).all()
        if not catalog_rows:
            return

        now = datetime.now(timezone.utc)
        deleted_total = 0
        for action_key, retention_days in catalog_rows:
            cutoff = now - timedelta(days=int(retention_days))
            # history_log: ``type`` field carries the action key.
            res = await db.execute(
                text("""
                    DELETE FROM history_log
                    WHERE type = :action_key
                      AND ts < :cutoff
                """),
                {"action_key": action_key, "cutoff": cutoff},
            )
            deleted_total += int(res.rowcount or 0)

            # credential_audit: ``action`` field, ``when_ts`` is a UNIX
            # timestamp (double precision), not a TIMESTAMPTZ. Convert
            # the cutoff so the comparison works.
            res = await db.execute(
                text("""
                    DELETE FROM credential_audit
                    WHERE action = :action_key
                      AND when_ts < :cutoff_unix
                """),
                {"action_key": action_key, "cutoff_unix": cutoff.timestamp()},
            )
            deleted_total += int(res.rowcount or 0)

        await db.commit()
        if deleted_total > 0:
            logger.info(
                "retention_keeper: deleted %d rows across history_log + credential_audit",
                deleted_total,
            )
