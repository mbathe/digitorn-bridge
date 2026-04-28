"""trusted_daemons table - central daemon auth bridge

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-26

Adds the registry of daemons allowed to mint Hub sessions on behalf
of their users via `POST /auth/daemon-bridge`. Each row stores a
single ed25519 public key (base64-encoded, 32 raw bytes) plus a
human-readable name used as the JWS-style key id.

The very first row, conventionally named `central`, is the digitorn.ai
production daemon. Self-hosted daemons MAY be added one-by-one but
the default policy is "central only".

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "hub"


def upgrade() -> None:
    op.create_table(
        "trusted_daemons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        # base64-encoded raw ed25519 public key (32 bytes -> 44 chars).
        sa.Column("public_key", sa.String(80), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(public_key) BETWEEN 32 AND 80",
            name="ck_trusted_daemons_pubkey_len",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_trusted_daemons_active",
        "trusted_daemons",
        ["name"],
        postgresql_where=sa.text("revoked_at IS NULL"),
        schema=SCHEMA,
    )

    # Anti-replay table - keeps every (daemon_name, nonce) we've seen
    # within the freshness window so a replayed signed request is
    # rejected. Rows are pruned by a simple `DELETE WHERE ts < now() -
    # interval` job; the index makes both sides cheap.
    op.create_table(
        "daemon_bridge_nonces",
        sa.Column("nonce", sa.String(64), primary_key=True),
        sa.Column("daemon_name", sa.String(80), nullable=False),
        sa.Column(
            "seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_daemon_bridge_nonces_seen_at",
        "daemon_bridge_nonces",
        ["seen_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("daemon_bridge_nonces", schema=SCHEMA)
    op.drop_table("trusted_daemons", schema=SCHEMA)
