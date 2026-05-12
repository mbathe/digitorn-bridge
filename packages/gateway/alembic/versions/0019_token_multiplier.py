"""Per-model token multiplier (Copilot-style premium request weighting).

A token is not a token: one Claude Opus token costs many times what one
Haiku token costs upstream. Counting raw tokens against a token-based
quota means a free-tier user spending all their budget on Opus burns
20-50x more dollars than the same user on Haiku - the cap is unfair.

Mirrors GitHub Copilot's "premium request multiplier" approach: each
catalogued model carries a ``token_multiplier``; the quota engine
multiplies the call's raw tokens by that factor before incrementing the
counters. A multiplier of 1.0 is the neutral default (no change).

Two additions:

  gateway_models.token_multiplier    NUMERIC(8, 4) NOT NULL DEFAULT 1.0
    Per-model weight. Apps configure this on the Models page when they
    register the alias. The quota engine reads it through the cache.

  gateway_usage_events.effective_tokens_total  INTEGER NOT NULL DEFAULT 0
    The row carries BOTH the raw token counts (prompt_tokens +
    completion_tokens, unchanged for cost-truth) AND the post-multiplier
    "billed tokens" (effective_tokens_total = raw * multiplier_applied).
    Storing the resolved number on the row is Option A: an audit point
    that survives later changes to the model's multiplier. If we only
    stored the multiplier and recomputed at read time, editing it would
    rewrite history.

Both default to a neutral value (1.0 / 0) so existing rows stay valid.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0019"
down_revision: Union[str, Sequence[str], None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "gateway_models",
        sa.Column(
            "token_multiplier",
            sa.Numeric(8, 4),
            nullable=False,
            server_default="1.0",
        ),
    )
    op.add_column(
        "gateway_usage_events",
        sa.Column(
            "effective_tokens_total",
            sa.BigInteger,
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("gateway_usage_events", "effective_tokens_total")
    op.drop_column("gateway_models", "token_multiplier")
