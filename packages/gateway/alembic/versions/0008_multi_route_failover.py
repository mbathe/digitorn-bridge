"""Multi-route failover: a model alias can have N ordered routes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-06

Until now ``gateway_routes.model_alias`` was the PK - one credential
per model. To support real-time failover (provider X is rate-limiting?
fall through to provider Y) we let several rows share a model alias,
each with its own ``priority``. The dispatcher walks them in
ascending priority and picks the first healthy one.

Schema changes:

  * Drop the existing PK on ``model_alias``.
  * Add ``id UUID`` PK (matches ``gateway_credentials``).
  * Add ``priority INT`` (lower = tried first).
  * UNIQUE on ``(model_alias, priority)`` so two routes can't tie.
  * Index on ``(model_alias, priority)`` for the cache reload loop.

Backfill: every existing row becomes priority=0 and gets a fresh UUID.

The migration is one-way - downgrade is destructive (would need to
collapse multiple rows per alias) so we leave it as a no-op.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # 1. Drop the old PK + add the new columns guarded.
    op.execute("""
    ALTER TABLE gateway_routes
        DROP CONSTRAINT IF EXISTS gateway_routes_pkey
    """)
    op.execute("""
    ALTER TABLE gateway_routes
        ADD COLUMN IF NOT EXISTS id UUID NOT NULL DEFAULT gen_random_uuid()
    """)
    op.execute("""
    ALTER TABLE gateway_routes
        ADD COLUMN IF NOT EXISTS priority INT NOT NULL DEFAULT 0
    """)
    op.execute("""
    ALTER TABLE gateway_routes
        ADD CONSTRAINT gateway_routes_pkey PRIMARY KEY (id)
    """)
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_gateway_routes_alias_priority
        ON gateway_routes (model_alias, priority)
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_gateway_routes_alias_priority_asc
        ON gateway_routes (model_alias, priority ASC)
    """)


def downgrade() -> None:
    if not _is_postgres():
        return
    # Destructive: would need to collapse N rows per alias to one.
    # Leave as no-op; restore from a snapshot if you really need it.
    pass
