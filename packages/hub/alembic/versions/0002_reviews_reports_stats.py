"""reviews, reports + aggregate cache on packages

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-26

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "hub"


def upgrade() -> None:
    # ── Aggregate cache on packages ───────────────────────────────
    op.add_column(
        "packages",
        sa.Column("avg_rating", sa.Numeric(3, 2), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "packages",
        sa.Column(
            "review_count",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        schema=SCHEMA,
    )

    # ── Reviews ────────────────────────────────────────────────────
    op.create_table(
        "reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "package_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.packages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rating", sa.SmallInteger, nullable=False),
        sa.Column("body", sa.Text, nullable=True),
        sa.Column("hidden", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("hidden_reason", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("package_id", "user_id", name="uq_reviews_pkg_user"),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_reviews_rating_range"),
        sa.CheckConstraint(
            "body IS NULL OR length(body) <= 4000",
            name="ck_reviews_body_max_len",
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_reviews_package_id", "reviews", ["package_id"], schema=SCHEMA)
    op.create_index("ix_reviews_user_id", "reviews", ["user_id"], schema=SCHEMA)
    op.create_index("ix_reviews_created_at", "reviews", ["created_at"], schema=SCHEMA)
    op.create_index(
        "ix_reviews_visible",
        "reviews",
        ["package_id", "created_at"],
        postgresql_where=sa.text("hidden = false"),
        schema=SCHEMA,
    )

    # ── Reports ────────────────────────────────────────────────────
    op.create_table(
        "reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "package_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.packages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reporter_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("details", sa.Text, nullable=True),
        sa.Column(
            "status", sa.String(20),
            nullable=False, server_default="open",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolution_note", sa.Text, nullable=True),
        sa.CheckConstraint(
            "reason IN ('malware','spam','abuse','copyright','broken','other')",
            name="ck_reports_reason",
        ),
        sa.CheckConstraint(
            "status IN ('open','reviewing','resolved','rejected')",
            name="ck_reports_status",
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_reports_package_id", "reports", ["package_id"], schema=SCHEMA)
    op.create_index("ix_reports_status", "reports", ["status"], schema=SCHEMA)
    op.create_index(
        "ix_reports_created_at", "reports", ["created_at"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_table("reports", schema=SCHEMA)
    op.drop_table("reviews", schema=SCHEMA)
    op.drop_column("packages", "review_count", schema=SCHEMA)
    op.drop_column("packages", "avg_rating", schema=SCHEMA)
