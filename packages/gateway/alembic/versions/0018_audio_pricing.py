"""Add audio transcription support: per-minute pricing + duration column.

Speech-to-text is priced per minute of audio (OpenAI Whisper-1 = $0.006/min,
Groq whisper-large-v3 = $0.111/hour ~= $0.00185/min, etc.) which is a
dimension orthogonal to the existing per-1k-token model. Two additions:

  gateway_models.cost_per_minute_audio   NUMERIC(12, 6) DEFAULT 0
    Per-minute price for transcription aliases. Stays 0 for chat-only
    models (which never get an audio call routed to them).

  gateway_usage_events.audio_seconds     NUMERIC(10, 2) DEFAULT 0
    Duration of the audio clip processed by this row. NULL/0 for
    non-transcription events. Used by aggregations to surface
    "minutes transcribed per provider" without parsing JSON.

Both default to 0 so existing rows stay valid. The audio path uses
``kind = 'transcription'`` on usage_events (the column already exists).

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0018"
down_revision: Union[str, Sequence[str], None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "gateway_models",
        sa.Column(
            "cost_per_minute_audio",
            sa.Numeric(12, 6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "gateway_usage_events",
        sa.Column(
            "audio_seconds",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("gateway_usage_events", "audio_seconds")
    op.drop_column("gateway_models", "cost_per_minute_audio")
