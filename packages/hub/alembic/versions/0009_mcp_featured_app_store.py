"""mcp_featured_entries: App-Store classification columns.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-15

Adds the three columns that turn the Hub catalog into a proper App
Store: every featured entry now declares which fields the **user**
fills (personal credentials only), which fields **Digitorn** provides
out-of-band (shared API keys, OAuth client ids, hosted bridges), and
optionally a Digitorn-hosted endpoint URL the daemon should default to.

All three columns are nullable / default-empty so live deployments
keep working unchanged. The companion code that consumes them lands
piece by piece post-prod (gateway shared-keys provisioning, install
dialog reformat, etc.).

Schema:

  * ``personal_keys``       text[]   subset of ``env_mapping`` keys the
                                     user must fill (their GitHub PAT,
                                     their Notion key). Empty = the
                                     server needs no user credentials.
  * ``digitorn_provided``   jsonb    {env_var_name -> credential ref}.
                                     The daemon resolves each ref via
                                     the gateway credentials store at
                                     install time and injects the value
                                     into the subprocess env. The user
                                     never sees these fields.
  * ``hosted_url``          text     Digitorn-managed deployment URL
                                     (e.g. our shared Cloudflare Worker
                                     bridge for github-webhook-mcp).
                                     Filled into the matching env var
                                     when the user's config is empty.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "hub"


def upgrade() -> None:
    op.add_column(
        "mcp_featured_entries",
        sa.Column(
            "personal_keys",
            postgresql.ARRAY(sa.String(120)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "mcp_featured_entries",
        sa.Column(
            "digitorn_provided",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "mcp_featured_entries",
        sa.Column("hosted_url", sa.String(512), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("mcp_featured_entries", "hosted_url", schema=SCHEMA)
    op.drop_column("mcp_featured_entries", "digitorn_provided", schema=SCHEMA)
    op.drop_column("mcp_featured_entries", "personal_keys", schema=SCHEMA)
