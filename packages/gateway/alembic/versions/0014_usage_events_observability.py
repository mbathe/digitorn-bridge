"""Add observability columns to gateway_usage_events.

The gateway now produces 5 runtime signals per dispatch (failover
trail, served-by, cache hit, truncation, multi-attempt). Persisting
them lets the dashboard answer questions you cannot answer today:

  * what % of requests survived a fallback?
  * which providers are the most reliable primary?
  * how often does the cache save a real LLM call?
  * how often does Mode 2 truncation kick in?

Columns:

  * served_by         text, NULL when unset (legacy rows)
  * attempts          smallint default 1
  * failover_trail    jsonb, list of provider slugs in attempt order;
                      NULL when no fallback was triggered
  * truncated_dropped smallint default 0 (Mode 2 head-drop block count)
  * cache_hit         boolean default false

The 5 columns are NULLable / default-valued so legacy partitions
keep working unchanged. The Postgres trigger that materialises
``total_cost_usd`` from ``cost_breakdown`` is untouched.

Indexed only on ``cache_hit`` (most-queried agg). The other columns
are filtered alongside ``ts`` which is already indexed; further
indexes can be added later from the dashboard's slow-query logs.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "gateway_usage_events",
        sa.Column("served_by", sa.Text(), nullable=True),
    )
    op.add_column(
        "gateway_usage_events",
        sa.Column(
            "attempts", sa.SmallInteger(),
            nullable=False, server_default="1",
        ),
    )
    op.add_column(
        "gateway_usage_events",
        sa.Column(
            "failover_trail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "gateway_usage_events",
        sa.Column(
            "truncated_dropped", sa.SmallInteger(),
            nullable=False, server_default="0",
        ),
    )
    op.add_column(
        "gateway_usage_events",
        sa.Column(
            "cache_hit", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
    )
    # Partial index: only rows that ARE cache hits. Tiny on disk, fast
    # for the "% cache hit" panel.
    op.create_index(
        "ix_gateway_usage_events_cache_hit",
        "gateway_usage_events",
        ["created_at", "user_id"],
        postgresql_where=sa.text("cache_hit = true"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_gateway_usage_events_cache_hit",
        table_name="gateway_usage_events",
    )
    op.drop_column("gateway_usage_events", "cache_hit")
    op.drop_column("gateway_usage_events", "truncated_dropped")
    op.drop_column("gateway_usage_events", "failover_trail")
    op.drop_column("gateway_usage_events", "attempts")
    op.drop_column("gateway_usage_events", "served_by")
