"""Add gateway_usage_events.agent_id column.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-05

The gateway records one row per LLM call. Until now those rows could
be attributed to (user_id, app_id, external_sid, run_id) but not to
the specific agent / specialist that issued the call. With multi-
specialist apps (a coordinator that delegates to ``code_reviewer``,
``researcher``, etc.), the cost-per-specialist breakdown wasn't
queryable.

This migration adds ``agent_id VARCHAR(64)`` to the partitioned
parent table. PostgreSQL propagates the column to every existing
partition automatically, and future partitions created via
``gateway_create_usage_partition()`` inherit it.

The column is nullable - rows persisted before this migration leave
``agent_id = NULL``, which the dashboard treats as "unknown specialist".
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    # The table is partitioned by RANGE(created_at); ALTER TABLE on the
    # parent automatically applies to every partition (current + future).
    op.execute("""
    ALTER TABLE gateway_usage_events
        ADD COLUMN IF NOT EXISTS agent_id VARCHAR(64)
    """)
    # Partial index for per-specialist drill-down. Standard CREATE INDEX
    # (not CONCURRENTLY) - the table is small at migration time and
    # non-concurrent index lets us stay inside one alembic transaction.
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_gateway_usage_events_agent_id
    ON gateway_usage_events (agent_id, created_at)
    WHERE agent_id IS NOT NULL
    """)


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute("DROP INDEX IF EXISTS ix_gateway_usage_events_agent_id")
    op.execute("ALTER TABLE gateway_usage_events DROP COLUMN IF EXISTS agent_id")
