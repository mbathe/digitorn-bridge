"""ensure user profile columns exist (avatar_url, phone, attributes, last_seen_at).

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-30

These columns are part of the auth-service's canonical User schema
(``digitorn_auth.models.User``). When the auth service shares Postgres
with the daemon, those columns may already exist (the daemon created
them during its own migrations). This migration is idempotent:
``ADD COLUMN IF NOT EXISTS`` makes it a no-op on already-up-to-date
schemas, and a real schema fix on fresh deployments.

Why we need this: the daemon used to own the ``users`` table, but the
identity refactor (2026-04-30) moves it under the auth service. Going
forward the auth service is the single writer; daemons read via
``GET /auth/me``. We bump the columns here so Postgres deployments
that started fresh from the auth-service migrations get the full
profile shape without depending on a daemon migration ever running.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS phone VARCHAR(64)"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(2048)"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS attributes JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE"
    )


def downgrade() -> None:
    # We don't drop the columns on downgrade - the daemon may still
    # rely on them. Downgrading the auth-service should never delete
    # data that other services read.
    pass
