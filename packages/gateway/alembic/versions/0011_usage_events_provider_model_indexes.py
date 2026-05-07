"""Indexes for per-provider / per-model usage analytics.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-07

Adds two indexes on the partitioned ``gateway_usage_events`` table to
make the new analytics endpoints sub-100ms instead of full-partition
scans:

  * ``(provider, created_at)`` - powers
    ``GET /admin/usage/top-providers`` and ``by-provider`` timelines.
  * ``(model, created_at)`` - powers
    ``GET /admin/usage/top-models`` and ``by-model`` timelines.

The indexes are added on the parent (declarative-partitioned) table.
Postgres propagates them to every existing partition AND any future
partition the partition_keeper creates - so we don't have to touch
``partition_keeper.py`` for the index plumbing (still updated to
mirror the legacy ``(user_id, created_at)`` pattern in case the
partition_keeper bypasses inheritance for hot-path reasons).

Hot-path impact: each ``INSERT INTO gateway_usage_events`` now walks
two extra B-trees (~+0.2 ms per insert). The insert runs in a
``BackgroundTask`` after the chat completion already streamed back
to the client - user-perceived latency stays at zero.

Migration safety: ``CREATE INDEX IF NOT EXISTS`` is idempotent. We
use the regular form (not ``CONCURRENTLY``) on the assumption that
the operator runs this during a maintenance window with no live
traffic. If you need a zero-downtime migration on a hot table, swap
to ``CREATE INDEX CONCURRENTLY`` and run outside a transaction.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_gateway_usage_events_provider_created
    ON gateway_usage_events (provider, created_at);
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_gateway_usage_events_model_created
    ON gateway_usage_events (model, created_at);
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_gateway_usage_events_provider_created;")
    op.execute("DROP INDEX IF EXISTS ix_gateway_usage_events_model_created;")
