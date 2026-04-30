"""initial: create paired_devices table.

Revision ID: 0001
Revises:
Create Date: 2026-04-30

This migration only creates the table that's BRAND NEW to the auth
service: ``paired_devices``. The other auth-owned tables
(``users``, ``user_oauth_tokens``, ``roles``, ``user_roles``,
``refresh_tokens``) already exist in the shared Postgres because
the daemon created them via its own migrations. Re-creating them
here would conflict; ``alembic stamp 0001`` on first deploy
records this revision as already-applied without touching the
existing schema.

For a brand-new standalone Postgres (e.g. dev/test SQLite), apply
``base`` first via ``Base.metadata.create_all`` (already done in
``server.py`` lifespan) then ``alembic stamp head`` so subsequent
migrations are picked up correctly.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "paired_devices",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("daemon_public_key", sa.Text(), nullable=True),
        sa.Column("last_token_jti", sa.String(64), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_ip", sa.String(64), nullable=True),
        sa.Column("last_seen_user_agent", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_paired_devices_user_id"),
        "paired_devices",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_paired_devices_user_id"), table_name="paired_devices")
    op.drop_table("paired_devices")
