"""Add per-user overage allowance column on gateway_user_plans.

Why:
  An admin (and later, the end user via their settings page) needs
  to grant ``extra_usage`` on top of a user's plan limit. The
  background quota supervisor consults this column when deciding
  whether to set a sticky block: an overflow is only blocked when
  ``actual > plan_limit + extra_usage``.

  The column is JSONB with the same shape as ``override_quota_def``
  (metric → window → amount) so the engine can reuse the existing
  rule-walking code. NULL = no overage (default).

  Designed to grow: the same JSONB cell can hold ``billing_mode``
  / ``cap`` / ``expires_at`` once we wire end-user self-service +
  Stripe metered billing.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "gateway_user_plans",
        sa.Column(
            "extra_usage_def",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("gateway_user_plans", "extra_usage_def")
