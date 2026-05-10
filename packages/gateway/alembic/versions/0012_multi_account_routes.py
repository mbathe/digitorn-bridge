"""Multi-account routing: drop unique(alias, priority), add unique(alias, credential_id).

Why:
  The previous unique constraint allowed only ONE route per
  (model_alias, priority) pair, which made multi-account routing
  impossible to configure via the standard CRUD API: an operator
  who wanted 5 Anthropic accounts behind a single alias for load
  balance had to set them at priorities 0..4 (strict failover, no
  load balance) instead of all at priority 0 (load balance within
  the tier).

  The resolver layer (config_cache.ConfigCache._resolve_route_at)
  has always supported multi-route-per-tier: it sorts the routes
  by (priority, inflight, consecutive_429s, route_id) and the
  dispatch loop picks the least-loaded credential. Only the DB +
  API enforced the single-route-per-priority invariant.

  This migration aligns the storage layer with the routing layer:
    * Drop ``uq_gateway_routes_alias_priority``
    * Add  ``uq_gateway_routes_alias_cred`` on
      (model_alias, credential_id) - a credential cannot be bound
      to the same alias twice (no functional reason to allow it),
      but several credentials can share priority within the alias.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-08
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the legacy unique. ``IF EXISTS`` keeps the migration
    # idempotent for environments where someone already dropped it
    # by hand (we did exactly that in our local debug session).
    op.execute(
        "ALTER TABLE gateway_routes "
        "DROP CONSTRAINT IF EXISTS uq_gateway_routes_alias_priority"
    )
    op.execute(
        "DROP INDEX IF EXISTS uq_gateway_routes_alias_priority"
    )
    # Pre-clean any duplicate (alias, credential_id) pair before
    # adding the new constraint, so the migration cannot fail on a
    # legacy duplicate row. Keep the most recent row, drop the rest.
    op.execute(
        """
        DELETE FROM gateway_routes a
        USING (
          SELECT model_alias, credential_id, MIN(updated_at) AS keep_ts
          FROM (
            SELECT model_alias, credential_id, updated_at,
                   ROW_NUMBER() OVER (
                     PARTITION BY model_alias, credential_id
                     ORDER BY updated_at DESC
                   ) AS rn
            FROM gateway_routes
          ) t
          WHERE rn > 1
          GROUP BY model_alias, credential_id
        ) dup
        WHERE a.model_alias = dup.model_alias
          AND a.credential_id = dup.credential_id
          AND a.updated_at < dup.keep_ts
        """
    )
    op.execute(
        "ALTER TABLE gateway_routes "
        "ADD CONSTRAINT uq_gateway_routes_alias_cred "
        "UNIQUE (model_alias, credential_id)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE gateway_routes "
        "DROP CONSTRAINT IF EXISTS uq_gateway_routes_alias_cred"
    )
    # Restore the old unique. Operators downgrading must accept that
    # multi-account routing breaks - same-priority duplicates created
    # while running on the new schema will block re-adding the old
    # constraint.
    op.execute(
        "ALTER TABLE gateway_routes "
        "ADD CONSTRAINT uq_gateway_routes_alias_priority "
        "UNIQUE (model_alias, priority)"
    )
