"""initial schema: users, publishers, packages, versions, tags, tokens, downloads

Revision ID: 0001
Revises:
Create Date: 2026-04-25

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from digitorn_hub.settings import get_settings

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMB_DIM = get_settings().embedding_dim
SCHEMA = "hub"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS {SCHEMA}')
    op.execute('CREATE EXTENSION IF NOT EXISTS "vector"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "pg_trgm"')

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(120), nullable=False),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint("role IN ('user','admin')", name="ck_users_role"),
        schema=SCHEMA,
    )
    op.create_index("ix_users_email", "users", ["email"], schema=SCHEMA)

    op.create_table(
        "api_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("token_hash", sa.String(120), nullable=False),
        sa.Column("prefix", sa.String(12), nullable=False),
        sa.Column(
            "scopes", postgresql.ARRAY(sa.String(40)),
            nullable=False, server_default="{}",
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"], schema=SCHEMA)
    op.create_index(
        "ix_api_tokens_token_hash", "api_tokens", ["token_hash"], schema=SCHEMA
    )

    op.create_table(
        "publishers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("bio", sa.Text),
        sa.Column("website", sa.String(255)),
        sa.Column("verified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("slug", name="uq_publishers_slug"),
        sa.CheckConstraint(
            r"slug ~ '^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$'",
            name="ck_publishers_slug_format",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_publishers_owner_user_id", "publishers", ["owner_user_id"], schema=SCHEMA
    )
    op.create_index("ix_publishers_slug", "publishers", ["slug"], schema=SCHEMA)

    op.create_table(
        "packages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "publisher_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.publishers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("package_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("category", sa.String(40)),
        sa.Column("risk_level", sa.String(10), nullable=False, server_default="medium"),
        sa.Column("icon_url", sa.String(512)),
        sa.Column("latest_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("total_downloads", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("search_vector", postgresql.TSVECTOR),
        sa.Column("embedding", Vector(EMB_DIM), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "publisher_id", "package_id", name="uq_packages_publisher_pkgid"
        ),
        sa.CheckConstraint(
            "risk_level IN ('low','medium','high')", name="ck_packages_risk_level"
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_packages_publisher_id", "packages", ["publisher_id"], schema=SCHEMA)
    op.create_index("ix_packages_package_id", "packages", ["package_id"], schema=SCHEMA)
    op.create_index("ix_packages_category", "packages", ["category"], schema=SCHEMA)
    op.create_index(
        "ix_packages_search_vector", "packages", ["search_vector"],
        postgresql_using="gin", schema=SCHEMA,
    )
    op.create_index(
        "ix_packages_name_trgm", "packages", ["name"],
        postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"},
        schema=SCHEMA,
    )
    op.create_index(
        "ix_packages_embedding", "packages", ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": "16", "ef_construction": "64"},
        postgresql_ops={"embedding": "vector_cosine_ops"},
        schema=SCHEMA,
    )

    op.create_table(
        "package_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "package_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.packages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB, nullable=False),
        sa.Column("archive_object_key", sa.String(512), nullable=False),
        sa.Column("archive_size", sa.BigInteger, nullable=False),
        sa.Column("archive_sha256", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("yanked", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("yanked_reason", sa.Text),
        sa.Column("yanked_at", sa.DateTime(timezone=True)),
        sa.Column("downloads", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "released_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "package_id", "version", name="uq_package_versions_pkg_ver"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_package_versions_package_id", "package_versions",
        ["package_id"], schema=SCHEMA,
    )

    op.create_foreign_key(
        "fk_packages_latest_version",
        "packages", "package_versions",
        ["latest_version_id"], ["id"],
        ondelete="SET NULL",
        source_schema=SCHEMA, referent_schema=SCHEMA,
    )

    op.create_table(
        "package_tags",
        sa.Column(
            "package_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.packages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tag", sa.String(40), primary_key=True),
        schema=SCHEMA,
    )
    op.create_index("ix_package_tags_tag", "package_tags", ["tag"], schema=SCHEMA)

    op.create_table(
        "download_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "package_version_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.package_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("daemon_fingerprint", sa.String(64)),
        sa.Column("ip_hash", sa.String(64)),
        sa.Column("user_agent", sa.String(255)),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_download_events_package_version_id", "download_events",
        ["package_version_id"], schema=SCHEMA,
    )
    op.create_index(
        "ix_download_events_occurred_at", "download_events",
        ["occurred_at"], schema=SCHEMA,
    )


def downgrade() -> None:
    op.execute(f'DROP SCHEMA IF EXISTS {SCHEMA} CASCADE')
