"""initial gateway schema (plans, user_plans, quota_counters, quota_blocks).

Revision ID: 0001
Revises:
Create Date: 2026-05-04

Creates the four gateway tables in the SHARED Postgres alongside the
auth tables. The `users` table itself MUST already exist - it is
owned by `digitorn-auth` and managed by its own Alembic chain.

Foreign keys:
* gateway_user_plans.user_id      -> users.id (CASCADE on delete)
* gateway_quota_counters.user_id  -> users.id (CASCADE)
* gateway_quota_blocks.user_id    -> users.id (CASCADE)
* gateway_user_plans.plan_id      -> gateway_plans.id (RESTRICT)

The `users.id` and gateway-side `user_id` columns are both
``VARCHAR(64)`` to match the hex-uuid format emitted by
``digitorn_auth.models.User._uuid``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── gateway_plans ─────────────────────────────────────────────
    op.create_table(
        "gateway_plans",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(512), nullable=True),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("quota_def", sa.JSON, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_gateway_plans_is_default", "gateway_plans", ["is_default"])

    # ── gateway_user_plans ───────────────────────────────────────
    op.create_table(
        "gateway_user_plans",
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "plan_id",
            sa.String(64),
            sa.ForeignKey("gateway_plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("override_quota_def", sa.JSON, nullable=True),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_gateway_user_plans_plan_id", "gateway_user_plans", ["plan_id"])

    # ── gateway_quota_counters ───────────────────────────────────
    op.create_table(
        "gateway_quota_counters",
        sa.Column(
            "id",
            sa.BigInteger,
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("window_key", sa.String(64), nullable=False),
        sa.Column("value", sa.Float, nullable=False, server_default="0"),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id", "metric", "window_key",
            name="uq_quota_counter_user_metric_window",
        ),
    )
    op.create_index(
        "ix_gateway_quota_counters_user_id",
        "gateway_quota_counters", ["user_id"],
    )
    op.create_index(
        "ix_gateway_quota_counters_reset_at",
        "gateway_quota_counters", ["reset_at"],
    )

    # ── gateway_quota_blocks ─────────────────────────────────────
    op.create_table(
        "gateway_quota_blocks",
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("window", sa.String(32), nullable=False),
        sa.Column("limit_value", sa.Float, nullable=False),
        sa.Column("actual_value", sa.Float, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_gateway_quota_blocks_blocked_until",
        "gateway_quota_blocks", ["blocked_until"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_gateway_quota_blocks_blocked_until", table_name="gateway_quota_blocks",
    )
    op.drop_table("gateway_quota_blocks")
    op.drop_index(
        "ix_gateway_quota_counters_reset_at", table_name="gateway_quota_counters",
    )
    op.drop_index(
        "ix_gateway_quota_counters_user_id", table_name="gateway_quota_counters",
    )
    op.drop_table("gateway_quota_counters")
    op.drop_index(
        "ix_gateway_user_plans_plan_id", table_name="gateway_user_plans",
    )
    op.drop_table("gateway_user_plans")
    op.drop_index("ix_gateway_plans_is_default", table_name="gateway_plans")
    op.drop_table("gateway_plans")
