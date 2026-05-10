"""Create gateway_runtime_settings: live, DB-backed feature flags.

Phase B of the Settings work. The dashboard's Settings page can now
toggle the operational flags without a gateway restart:

  * The PUT endpoint upserts a row here AND mutates the live
    ``Settings`` singleton in the worker process.
  * On boot, ``runtime_settings_loader`` reads every row and overrides
    the matching field on Settings BEFORE any traffic is served.

Schema is intentionally minimal: one row per flag with a JSONB value.
JSONB lets us hold scalars (bool, int, str) and any future structured
override (lists, nested config) without a schema change.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gateway_runtime_settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("gateway_runtime_settings")
