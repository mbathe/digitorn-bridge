"""packages.icon_storage_ext — extension hint for Hub-served icons

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-26

Pure additive: stores the file extension (`png|jpg|gif|webp|svg|ico`)
of icons we've uploaded to the private S3 prefix `icons/{publisher}/
{package}.{ext}`. The icon route uses this to compose the S3 key
without needing a list_objects call.

`icon_url` keeps holding the URL the client should fetch — either
`{hub_public_base}/api/v1/packages/{pub}/{pkg}/icon` (Hub-served)
or an absolute publisher-hosted URL.

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "hub"


def upgrade() -> None:
    op.add_column(
        "packages",
        sa.Column("icon_storage_ext", sa.String(8), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("packages", "icon_storage_ext", schema=SCHEMA)
