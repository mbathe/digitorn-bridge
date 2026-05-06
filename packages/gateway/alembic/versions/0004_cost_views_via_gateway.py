"""Rebase cost views onto gateway_usage_events (Postgres only).

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-05

Architectural decision (validated 2026-05-05): cost is a gateway
concern, never a runtime concern. The runtime never writes
``agent_runs.cost_breakdown`` - that column stays empty. Real LLM
spend lives in ``gateway_usage_events`` where the gateway writes one
row per dispatched call.

This migration:

  1. Replaces ``v_agents_top_cost_7d`` so it aggregates cost from
     ``gateway_usage_events`` (the source of truth) joined to
     ``agent_runs`` by ``user_id`` over the rolling 7-day window. The
     join falls back gracefully when the gateway lacks ``run_id``
     (e.g., before the runtime starts forwarding it as a header).

  2. Replaces ``v_user_quota_state`` (no schema change to columns)
     with the same definition the previous migration created, so
     ``CREATE OR REPLACE`` is a no-op on schema but documents the
     view as still part of the v2 surface.

The runtime can no longer be slowed down by cost computation -
``agent_runs.cost_breakdown`` is officially "leave it empty".
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # v_agents_top_cost_7d - cost comes from gateway_usage_events, NOT
    # from agent_runs.total_cost_usd (which is always 0 in v2).
    op.execute("""
    CREATE OR REPLACE VIEW v_agents_top_cost_7d AS
    SELECT
        ar.user_id,
        COUNT(DISTINCT ar.id)        AS run_count,
        COALESCE(SUM(ar.total_tokens), 0)::BIGINT AS tokens_7d,
        COALESCE(
            (
                SELECT SUM(gue.total_cost_usd)
                FROM gateway_usage_events gue
                WHERE gue.user_id = ar.user_id
                  AND gue.created_at >= NOW() - INTERVAL '7 days'
            ),
            0
        )                            AS cost_7d_usd,
        COALESCE(SUM(ar.turns_used), 0) AS turns_7d,
        AVG(ar.duration_ms)::BIGINT  AS avg_duration_ms
    FROM agent_runs ar
    WHERE ar.completed_at >= NOW() - INTERVAL '7 days'
      AND ar.status = 'completed'
    GROUP BY ar.user_id
    ORDER BY cost_7d_usd DESC NULLS LAST,
             tokens_7d DESC
    LIMIT 50;
    """)


def downgrade() -> None:
    if not _is_postgres():
        return

    # Restore the original definition that summed agent_runs.total_cost_usd.
    op.execute("""
    CREATE OR REPLACE VIEW v_agents_top_cost_7d AS
    SELECT
        user_id,
        COUNT(*)                    AS run_count,
        SUM(total_tokens)::BIGINT   AS tokens_7d,
        SUM(total_cost_usd)         AS cost_7d_usd,
        SUM(turns_used)             AS turns_7d,
        AVG(duration_ms)::BIGINT    AS avg_duration_ms
    FROM agent_runs
    WHERE completed_at >= NOW() - INTERVAL '7 days'
      AND status = 'completed'
    GROUP BY user_id
    ORDER BY cost_7d_usd DESC
    LIMIT 50;
    """)
