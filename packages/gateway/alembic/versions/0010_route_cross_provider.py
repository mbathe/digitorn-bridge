"""Cross-provider routing: routes carry their own dispatch identity.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-07

Each row in ``gateway_routes`` becomes self-contained: it carries
``provider_slug``, ``real_model_id``, ``compat``, ``base_url`` and
``dispatch_headers`` directly, instead of inheriting them from the
``gateway_models`` row of its alias. This unlocks fail-over to a
DIFFERENT provider (e.g. primary github_copilot, fallback anthropic).

The ``gateway_models`` row stays as a metadata anchor (cost + context
window + alias display) but no longer dictates the dispatch target.

Backfill: every existing route copies the alias's current
``(provider_slug, real_model_id, compat, base_url, dispatch_headers)``,
so behaviour is byte-for-byte identical the moment the migration
finishes. ``base_url`` and ``dispatch_headers`` come from the provider
row (the provider owns those today; routes only override when the
operator wants a different endpoint or headers).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # 1. Add columns nullable + with defaults so existing rows can
    #    be backfilled in-place without breaking the route handlers
    #    if they are running during the deploy.
    op.execute("""
    ALTER TABLE gateway_routes
        ADD COLUMN IF NOT EXISTS provider_slug VARCHAR(64),
        ADD COLUMN IF NOT EXISTS real_model_id TEXT,
        ADD COLUMN IF NOT EXISTS compat VARCHAR(32),
        ADD COLUMN IF NOT EXISTS base_url TEXT,
        ADD COLUMN IF NOT EXISTS dispatch_headers JSONB NOT NULL DEFAULT '{}'::jsonb
    """)

    # 2. Backfill from the model + provider rows. The model owns
    #    (provider_slug, real_model_id); the provider owns (compat,
    #    base_url, metadata.dispatch_headers). Every existing route
    #    inherits the current state.
    op.execute("""
    UPDATE gateway_routes r
       SET provider_slug    = COALESCE(r.provider_slug, m.provider_slug),
           real_model_id    = COALESCE(r.real_model_id, m.real_model_id),
           compat           = COALESCE(r.compat, p.compat),
           base_url         = COALESCE(r.base_url, p.base_url),
           dispatch_headers = COALESCE(
               NULLIF(r.dispatch_headers, '{}'::jsonb),
               COALESCE(p.metadata->'dispatch_headers', '{}'::jsonb)
           )
      FROM gateway_models m
      JOIN gateway_providers p ON p.slug = m.provider_slug
     WHERE r.model_alias = m.alias
    """)

    # 3. Lock the new columns down. Any row that didn't backfill
    #    (orphaned route pointing at an archived model) will trip
    #    the NOT NULL constraint -- fail loudly so the operator
    #    fixes their data instead of silently shipping a broken
    #    catalogue.
    op.execute("""
    ALTER TABLE gateway_routes
        ALTER COLUMN provider_slug SET NOT NULL,
        ALTER COLUMN real_model_id SET NOT NULL,
        ALTER COLUMN compat SET NOT NULL
    """)

    # 4. FK + index for the routing-by-provider lookups (e.g. "show
    #    me every route that targets github_copilot").
    op.execute("""
    ALTER TABLE gateway_routes
        ADD CONSTRAINT gateway_routes_provider_fk
        FOREIGN KEY (provider_slug)
        REFERENCES gateway_providers(slug)
        ON DELETE RESTRICT
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_gateway_routes_provider_slug
        ON gateway_routes (provider_slug)
    """)


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute("""
    ALTER TABLE gateway_routes
        DROP CONSTRAINT IF EXISTS gateway_routes_provider_fk
    """)
    op.execute("DROP INDEX IF EXISTS ix_gateway_routes_provider_slug")
    op.execute("""
    ALTER TABLE gateway_routes
        DROP COLUMN IF EXISTS provider_slug,
        DROP COLUMN IF EXISTS real_model_id,
        DROP COLUMN IF EXISTS compat,
        DROP COLUMN IF EXISTS base_url,
        DROP COLUMN IF EXISTS dispatch_headers
    """)
