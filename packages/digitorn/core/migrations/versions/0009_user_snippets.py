"""User snippets: per-user, per-app reusable prompt templates.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-16

Stores the prompt templates the chat composer's "Insert snippet" menu
hands the user. Scoped per ``(user_id, app_id)`` so each agent has its
own personal library — the snippets the user builds while talking to
``copilot-smoke`` don't appear when they switch to ``digitorn-code``.

Columns:

  - ``id``: UUID primary key.
  - ``user_id`` / ``app_id``: scope.
  - ``title``: short human label rendered in the picker.
  - ``body``: the prompt content, may contain ``{{variable}}``
    placeholders that the composer cycles through with Tab.
  - ``emoji``: optional decoration (UI chip prefix).
  - ``tags``: optional JSON array of strings for future filtering.
  - ``created_at`` / ``updated_at``: standard audit columns.

Idempotent: ``CREATE TABLE IF NOT EXISTS`` is implicit because the
inspector check skips the create when the table already exists.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return name in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "user_snippets"):
        return
    op.create_table(
        "user_snippets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("app_id", sa.String(128), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("emoji", sa.String(16), nullable=True),
        sa.Column("tags", sa.JSON, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Composite index: the most common query is "list this user's
    # snippets for this app, newest first". A single (user_id, app_id)
    # index covers the WHERE; ordering by updated_at is cheap on
    # ≤ N hundreds of rows so we skip an extra trailing column.
    op.create_index(
        "ix_user_snippets_user_app",
        "user_snippets",
        ["user_id", "app_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "user_snippets"):
        return
    op.drop_index("ix_user_snippets_user_app", table_name="user_snippets")
    op.drop_table("user_snippets")
