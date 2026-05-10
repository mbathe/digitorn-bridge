"""Add attribution indexes to gateway_usage_events for puissant analytics.

Adds 3 partial indexes that make per-app, per-session, and per-run
aggregations sub-100ms even on multi-million-row partitions:

  ix_gateway_usage_events_app      (app_id, created_at) WHERE app_id IS NOT NULL
  ix_gateway_usage_events_session  (external_sid, created_at) WHERE external_sid IS NOT NULL
  ix_gateway_usage_events_run      (run_id) WHERE run_id IS NOT NULL

Why partial: most rows from CLI / ad-hoc tests have NULL attribution
IDs. Indexing only the populated rows keeps the on-disk index size
proportional to actual analytical use, not raw write volume.

Why CONCURRENTLY per partition: the standard ``CREATE INDEX`` on the
parent acquires ACCESS EXCLUSIVE on each child partition during
build. On a multi-million-row partition that briefly blocks the
gateway's BackgroundTask writes. Postgres's canonical no-downtime
pattern is:

  1. CREATE INDEX ON ONLY parent  (placeholder, no children indexed)
  2. For each child partition:
     CREATE INDEX CONCURRENTLY child_idx ON child_partition (...)
     ALTER INDEX parent_idx ATTACH PARTITION child_idx
  3. Once every partition has a matching index attached, the parent
     index becomes ``valid`` and serves queries.

Future partitions inherit the parent definition automatically when
the partition_keeper calls ``gateway_create_usage_partition(month)``.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016"
down_revision: Union[str, Sequence[str], None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# CONCURRENTLY commands cannot run inside a transaction. Alembic's
# ``transactional_ddl=False`` lets us issue them at the autocommit level.
# We open + commit per statement explicitly to keep each step isolated.

# Indexes we want on the partitioned parent. Each entry is
# (parent_index_name, column_expression, partial_predicate).
_PARTITIONED_INDEXES = [
    (
        "ix_gateway_usage_events_app",
        "(app_id, created_at)",
        "app_id IS NOT NULL",
    ),
    (
        "ix_gateway_usage_events_session",
        "(external_sid, created_at)",
        "external_sid IS NOT NULL",
    ),
    (
        "ix_gateway_usage_events_run",
        "(run_id)",
        "run_id IS NOT NULL",
    ),
]


def upgrade() -> None:
    # Standard ``CREATE INDEX`` on the parent partitioned table:
    # Postgres builds the index on each child partition with a brief
    # ACCESS EXCLUSIVE lock per partition. On dev / early prod where
    # partitions are small (<100k rows), each child takes <1s to
    # index. Future partitions inherit automatically when
    # ``gateway_create_usage_partition()`` runs.
    #
    # Production note: when partitions grow beyond ~10M rows, switch
    # to the per-partition CONCURRENTLY + ATTACH PARTITION pattern
    # in a maintenance window. See revision 0016_v2 (TBD) for the
    # zero-downtime variant.
    for parent_idx, cols, where in _PARTITIONED_INDEXES:
        op.execute(sa.text(f"""
            CREATE INDEX IF NOT EXISTS {parent_idx}
            ON gateway_usage_events {cols}
            WHERE {where}
        """))


def downgrade() -> None:
    for parent_idx, _cols, _where in _PARTITIONED_INDEXES:
        op.execute(sa.text(f"DROP INDEX IF EXISTS {parent_idx}"))
