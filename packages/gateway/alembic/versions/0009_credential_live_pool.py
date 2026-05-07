"""Connection pooling toggle on each credential.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-07

Adds ``gateway_credentials.live_pool`` (bool, default true) so the
runtime can keep an httpx.AsyncClient warm per credential and pass it
to ``litellm.acompletion(client=..., ...)`` -- saving the TCP + TLS
handshake (typically 100-300ms RTT) on every dispatch.

Default true so newly-created credentials get the speed-up
automatically; operators can opt out via the dashboard toggle (e.g.
memory-constrained deployments). For Bedrock / Vertex auth_types the
pool is a no-op at the dispatch layer (boto3 / google-auth own their
connection caching), but we still store the column so the schema
stays uniform.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "gateway_credentials",
        sa.Column(
            "live_pool",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("gateway_credentials", "live_pool")
