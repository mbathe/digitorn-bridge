"""initial: ensure paired_devices, revoked_tokens, account_features exist.

Revision ID: 0001
Revises:
Create Date: 2026-04-30

Creates the three tables this auth service owns. The other auth-related
tables (``users``, ``user_oauth_tokens``, ``roles``, ``user_roles``,
``refresh_tokens``) already exist in the shared Postgres because the
daemon created them via its own migrations - they are intentionally
NOT touched here.

**Idempotent**: each statement is wrapped in ``CREATE ... IF NOT EXISTS``
so the migration succeeds in three scenarios:
  1. Fresh Postgres (no auth tables) → tables get created.
  2. Daemon-created Postgres + auth ran ``Base.metadata.create_all``
     against it once (test runs, dev cycle) → tables already there,
     ``IF NOT EXISTS`` no-ops, alembic_version row gets stamped.
  3. Re-applying after manual schema fix → still safe.

The downgrade still drops everything, on the assumption that you only
roll back when you really want to undo.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # paired_devices
    op.execute("""
        CREATE TABLE IF NOT EXISTS paired_devices (
            id                    VARCHAR(64) PRIMARY KEY,
            user_id               VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            label                 VARCHAR(128) NOT NULL,
            daemon_public_key     TEXT,
            last_token_jti        VARCHAR(64),
            last_seen_at          TIMESTAMP WITH TIME ZONE,
            last_seen_ip          VARCHAR(64),
            last_seen_user_agent  VARCHAR(255),
            created_at            TIMESTAMP WITH TIME ZONE NOT NULL,
            revoked_at            TIMESTAMP WITH TIME ZONE
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_paired_devices_user_id "
        "ON paired_devices (user_id)"
    )

    # revoked_tokens
    op.execute("""
        CREATE TABLE IF NOT EXISTS revoked_tokens (
            jti          VARCHAR(64) PRIMARY KEY,
            user_id      VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at   TIMESTAMP WITH TIME ZONE NOT NULL,
            reason       VARCHAR(64) NOT NULL DEFAULT 'user_logout',
            revoked_at   TIMESTAMP WITH TIME ZONE NOT NULL
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_revoked_tokens_user_id "
        "ON revoked_tokens (user_id)"
    )

    # account_features (1:1 with users via PK)
    op.execute("""
        CREATE TABLE IF NOT EXISTS account_features (
            user_id                     VARCHAR(64) PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            plan_tier                   VARCHAR(32) NOT NULL DEFAULT 'free',
            cloud_enabled               BOOLEAN NOT NULL DEFAULT FALSE,
            self_host_enabled           BOOLEAN NOT NULL DEFAULT TRUE,
            cloud_token_quota_monthly   INTEGER NOT NULL DEFAULT 0,
            max_paired_devices          INTEGER NOT NULL DEFAULT 5,
            flags                       JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at                  TIMESTAMP WITH TIME ZONE NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS account_features")
    op.execute("DROP TABLE IF EXISTS revoked_tokens")
    op.execute("DROP INDEX IF EXISTS ix_paired_devices_user_id")
    op.execute("DROP TABLE IF EXISTS paired_devices")
