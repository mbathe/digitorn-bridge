"""Add deployer_jwt column on applications.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-27

Stores the bearer of the user who deployed the app. Used at cron
auto-fire time when there is no inbound HTTP request to grab a JWT
from, so the daemon can still issue an authenticated call to the
local digitorn gateway on behalf of that user.

Encrypted at rest via `_EncryptedJSON` (LargeBinary + crypto.encrypt_value).
Apps deployed before this migration will have `deployer_jwt = NULL` and
will keep 401-ing on cron auto-fire until they are re-deployed.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = inspect(bind)
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "applications", "deployer_jwt"):
        return
    op.add_column(
        "applications",
        sa.Column("deployer_jwt", sa.LargeBinary, nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "applications", "deployer_jwt"):
        return
    op.drop_column("applications", "deployer_jwt")
