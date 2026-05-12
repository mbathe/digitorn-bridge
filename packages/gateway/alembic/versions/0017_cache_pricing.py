"""Add cache pricing columns to gateway_models.

Until now ``cost_per_1k_input_tokens`` was applied to ALL prompt
tokens, regardless of whether they hit the upstream's prompt cache.
That over-bills callers whose workload benefits from caching (cache
reads are 25-90% cheaper depending on provider) and under-bills
callers writing to cache on Anthropic (25% premium).

Two new columns, both DEFAULT 0 to honour the "if not provided, no
charge" policy:

  cost_per_1k_cache_read_tokens   - applied to cached input tokens
                                    (OpenAI cached_tokens, Anthropic
                                    cache_read_input_tokens)
  cost_per_1k_cache_write_tokens  - applied to cache-creation tokens
                                    (Anthropic cache_creation_input_tokens,
                                    Gemini cache_create_input_tokens)

When both columns are 0 (default for fresh install + providers
without cache), the new cost formula collapses to the previous
behaviour. Operators MUST explicitly set these via the dashboard or
the catalog seed endpoint to get cache-aware billing.

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0017"
down_revision: Union[str, Sequence[str], None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NUMERIC(12, 6) to match the existing input/output cost columns.
    # Float would have produced DOUBLE PRECISION which interacts
    # awkwardly with SUM() against the existing fixed-point columns.
    op.add_column(
        "gateway_models",
        sa.Column(
            "cost_per_1k_cache_read_tokens",
            sa.Numeric(12, 6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "gateway_models",
        sa.Column(
            "cost_per_1k_cache_write_tokens",
            sa.Numeric(12, 6),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("gateway_models", "cost_per_1k_cache_write_tokens")
    op.drop_column("gateway_models", "cost_per_1k_cache_read_tokens")
