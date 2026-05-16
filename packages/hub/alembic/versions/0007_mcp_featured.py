"""mcp_featured_entries - curated, admin-editable MCP catalog

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-15

Per the user-facing "App Store" model: the Hub is the single source of
truth for MCP server metadata. Each per-user daemon reads this table
through a proxy + 5 min cache. Admins manage entries through CRUD
endpoints — no daemon redeploy required to add/remove a server.

The table mirrors the ``CatalogEntry`` dataclass from
``packages/digitorn/modules/mcp/catalog.py``, which becomes the
last-resort offline fallback for daemons that can't reach the Hub.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "hub"


def upgrade() -> None:
    op.create_table(
        "mcp_featured_entries",
        sa.Column("server_id", sa.String(120), primary_key=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("icon", sa.String(32), nullable=False, server_default=""),
        sa.Column("category", sa.String(80), nullable=False, server_default=""),

        # Install config
        sa.Column("transport", sa.String(20), nullable=False, server_default="stdio"),
        sa.Column("command", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "args",
            postgresql.ARRAY(sa.String(512)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column("runtime", sa.String(20), nullable=False, server_default="npm"),
        sa.Column("package", sa.String(255), nullable=False, server_default=""),
        sa.Column("url", sa.String(512)),
        sa.Column(
            "default_env",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),

        # Auth wiring (shorthand key → real env var name + per-key help text)
        sa.Column(
            "env_mapping",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "key_descriptions",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("oauth_provider", sa.String(80)),
        sa.Column("oauth_style", sa.String(40), nullable=False, server_default=""),
        sa.Column("oauth_env_token_var", sa.String(120), nullable=False, server_default=""),
        sa.Column(
            "oauth_scopes",
            postgresql.ARRAY(sa.String(255)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column("oauth_keyfile_env", sa.String(120), nullable=False, server_default=""),
        sa.Column("oauth_credentials_env", sa.String(120), nullable=False, server_default=""),
        sa.Column("oauth_credentials_filename", sa.String(120), nullable=False, server_default=""),

        sa.Column("binary_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("smithery_slug", sa.String(120), nullable=False, server_default=""),
        sa.Column("timeout", sa.Float, nullable=False, server_default="30.0"),

        # Curation
        sa.Column("featured_priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("hidden", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("verified_by", sa.String(255)),
        sa.Column("last_tested_at", sa.DateTime(timezone=True)),
        sa.Column("last_tested_ok", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("last_test_error", sa.Text),

        # Optional link to the firehose mirror
        sa.Column("registry_server_id", sa.String(120)),

        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),

        sa.CheckConstraint(
            "runtime IN ('npm','pip','uv','docker','remote','custom','none')",
            name="ck_mcp_featured_runtime",
        ),
        sa.CheckConstraint(
            "transport IN ('stdio','sse','streamable_http','http','ws')",
            name="ck_mcp_featured_transport",
        ),
        schema=SCHEMA,
    )

    op.create_index(
        "ix_mcp_featured_category", "mcp_featured_entries",
        ["category"], schema=SCHEMA,
    )
    op.create_index(
        "ix_mcp_featured_priority_hidden", "mcp_featured_entries",
        ["hidden", "featured_priority"], schema=SCHEMA,
    )
    op.create_index(
        "ix_mcp_featured_oauth_provider", "mcp_featured_entries",
        ["oauth_provider"], schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("mcp_featured_entries", schema=SCHEMA)
