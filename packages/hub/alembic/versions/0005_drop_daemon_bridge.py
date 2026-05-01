"""drop trusted_daemons + daemon_bridge_nonces

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-01

Retires the daemon-bridge HMAC auth flow. The Hub now accepts the
central RS256 JWT issued by ``auth.digitorn.ai`` (see
``digitorn_hub.auth.central``), so the trusted-daemon table and the
nonce replay-cache that backed the bridge are no longer needed.

Dropping these tables is destructive and irreversible. Verify there
are no daemons still using the bridge before applying. The downgrade
recreates empty tables as a courtesy but the data is gone.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS daemon_bridge_nonces CASCADE")
    op.execute("DROP TABLE IF EXISTS trusted_daemons CASCADE")


def downgrade() -> None:
    # Empty stubs - the bridge feature has been physically removed
    # from the codebase, recreating the tables wouldn't restore the
    # functionality. Provided for symmetry only.
    op.create_table(
        "trusted_daemons",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("public_key", sa.String(80), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "daemon_bridge_nonces",
        sa.Column("nonce", sa.String(64), primary_key=True),
        sa.Column("daemon_name", sa.String(80), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
