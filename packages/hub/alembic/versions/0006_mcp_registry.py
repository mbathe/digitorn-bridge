"""mcp_registry_entries — cached mirror of registry.modelcontextprotocol.io

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-15

Adds one table: ``hub.mcp_registry_entries``. The Hub periodically
fetches the official MCP registry (every 24h) and upserts the flat
shape used by the dashboard:

  * ``server_id``           short slug (PK)
  * ``name``                upstream fully-qualified name
  * ``description``
  * ``runtime`` / ``package`` / ``transport``
  * ``has_oauth``           bool
  * ``required_env_count``  int
  * ``env_var_names``       array
  * ``raw_metadata``        full upstream JSON (for future flattening)
  * ``search_vector``       FTS (GIN)
  * ``embedding``           384-dim, HNSW cosine

Daemons proxy ``/api/mcp/registry/*`` to the Hub instead of hitting
the upstream registry directly, so the daemon hot path never pays the
network cost of the registry call.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from digitorn_hub.settings import get_settings

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMB_DIM = get_settings().embedding_dim
SCHEMA = "hub"


def upgrade() -> None:
    op.create_table(
        "mcp_registry_entries",
        sa.Column("server_id", sa.String(120), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("runtime", sa.String(20), nullable=False, server_default="none"),
        sa.Column("package", sa.String(255)),
        sa.Column("transport", sa.String(20), nullable=False, server_default="stdio"),
        sa.Column("has_oauth", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "required_env_count", sa.Integer, nullable=False, server_default="0",
        ),
        sa.Column(
            "env_var_names",
            postgresql.ARRAY(sa.String(120)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column("version", sa.String(40)),
        sa.Column("repository_url", sa.String(512)),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column(
            "raw_metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("search_vector", postgresql.TSVECTOR),
        sa.Column("embedding", Vector(EMB_DIM), nullable=True),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "runtime IN ('npm','pip','none','custom')",
            name="ck_mcp_registry_runtime",
        ),
        sa.CheckConstraint(
            "transport IN ('stdio','sse','streamable_http','http','ws')",
            name="ck_mcp_registry_transport",
        ),
        sa.CheckConstraint(
            "status IN ('active','deprecated','retired')",
            name="ck_mcp_registry_status",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_mcp_registry_search_vector",
        "mcp_registry_entries",
        ["search_vector"],
        postgresql_using="gin",
        schema=SCHEMA,
    )
    op.create_index(
        "ix_mcp_registry_name_trgm",
        "mcp_registry_entries",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
        schema=SCHEMA,
    )
    op.create_index(
        "ix_mcp_registry_embedding",
        "mcp_registry_entries",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": "16", "ef_construction": "64"},
        postgresql_ops={"embedding": "vector_cosine_ops"},
        schema=SCHEMA,
    )
    op.create_index(
        "ix_mcp_registry_runtime", "mcp_registry_entries",
        ["runtime"], schema=SCHEMA,
    )
    op.create_index(
        "ix_mcp_registry_status", "mcp_registry_entries",
        ["status"], schema=SCHEMA,
    )
    op.create_index(
        "ix_mcp_registry_last_seen_at", "mcp_registry_entries",
        ["last_seen_at"], schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("mcp_registry_entries", schema=SCHEMA)
