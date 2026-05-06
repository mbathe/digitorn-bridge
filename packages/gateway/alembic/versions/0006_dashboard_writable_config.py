"""Dashboard-writable gateway config: providers, credentials, models, routes.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-06

Until this migration the gateway's provider/model config lived in the
process: ``_PROVIDER_ENV_KEYS`` (Python literal) + ``models.yaml``
(disk file) + ``ANTHROPIC_API_KEY`` etc (env vars). Operators had to
edit code/files and restart for the smallest change.

This migration introduces 4 tables that mirror what the admin
dashboard needs to mutate at runtime:

- ``gateway_providers``: declared LLM providers (slug, name, base_url,
  compat dialect). The defaults at boot replicate the legacy hard-coded
  list so existing deployments keep working.
- ``gateway_credentials``: encrypted API keys per provider. The
  ``encrypted_value`` is an opaque blob the cipher module wraps with
  the gateway's master key (envelope encryption). We never expose the
  decrypted value through the API.
- ``gateway_models``: catalogue (alias -> provider/model + pricing).
  Replaces ``models.yaml`` as the source of truth once seeded.
- ``gateway_routes``: which credential to use for each model. Lets
  ops "rotate by route" without touching the credential row.

Boot-time seeding is handled by the gateway lifespan (NOT this
migration) so the YAML/env legacy config keeps working transparently.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # gateway_providers
    op.execute("""
    CREATE TABLE IF NOT EXISTS gateway_providers (
        slug          VARCHAR(64) PRIMARY KEY,
        name          VARCHAR(128) NOT NULL,
        base_url      TEXT,
        compat        VARCHAR(32) NOT NULL DEFAULT 'openai',
        env_var       VARCHAR(64),
        metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
        archived_at   TIMESTAMPTZ,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)

    # gateway_credentials
    op.execute("""
    CREATE TABLE IF NOT EXISTS gateway_credentials (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        provider_slug   VARCHAR(64) NOT NULL
                          REFERENCES gateway_providers(slug) ON DELETE CASCADE,
        label           VARCHAR(128) NOT NULL,
        encrypted_value BYTEA NOT NULL,
        cipher_version  SMALLINT NOT NULL DEFAULT 1,
        status          VARCHAR(16) NOT NULL DEFAULT 'active',
        last_used_at    TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by      VARCHAR(64),
        UNIQUE (provider_slug, label)
    )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_gateway_credentials_provider_status "
        "ON gateway_credentials (provider_slug, status)"
    )

    # gateway_models
    op.execute("""
    CREATE TABLE IF NOT EXISTS gateway_models (
        alias                       VARCHAR(128) PRIMARY KEY,
        provider_slug               VARCHAR(64) NOT NULL
                                      REFERENCES gateway_providers(slug)
                                      ON DELETE RESTRICT,
        real_model_id               TEXT NOT NULL,
        cost_per_1k_input_tokens    NUMERIC(12,6) NOT NULL DEFAULT 0,
        cost_per_1k_output_tokens   NUMERIC(12,6) NOT NULL DEFAULT 0,
        max_context_tokens          INTEGER,
        is_custom                   BOOLEAN NOT NULL DEFAULT FALSE,
        metadata                    JSONB NOT NULL DEFAULT '{}'::jsonb,
        archived_at                 TIMESTAMPTZ,
        created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_gateway_models_provider "
        "ON gateway_models (provider_slug)"
    )

    # gateway_routes (one credential per model alias)
    op.execute("""
    CREATE TABLE IF NOT EXISTS gateway_routes (
        model_alias    VARCHAR(128) PRIMARY KEY
                         REFERENCES gateway_models(alias) ON DELETE CASCADE,
        credential_id  UUID NOT NULL
                         REFERENCES gateway_credentials(id) ON DELETE RESTRICT,
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute("DROP TABLE IF EXISTS gateway_routes")
    op.execute("DROP TABLE IF EXISTS gateway_models")
    op.execute("DROP TABLE IF EXISTS gateway_credentials")
    op.execute("DROP TABLE IF EXISTS gateway_providers")
