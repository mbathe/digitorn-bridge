"""User skills: per-user, per-app authored skills with system-prompt injection.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-16

Stores the skill instructions the chat composer's `/use_skill <name>
<prompt>` syntax injects as a turn-scoped `role: system` directive.
Distinct from `dev.skills` (declared in YAML by the app author, loaded
from .md files at compile time) - these are owned by the end user and
gated behind `dev.allow_user_skills: true` in the app YAML.

Scoped per `(user_id, app_id)` so each agent has its own personal
skill library - the skills a user authors while talking to
`digitorn-code` don't appear when they switch to `digitorn-chat`.

Columns:

  - `id`: UUID primary key.
  - `user_id` / `app_id`: scope.
  - `name`: short slug used as the skill identifier, e.g.
    `commit`. The composer surfaces `/<name>` in the palette; the
    daemon's `/use_skill` parser matches against `name` directly.
  - `description`: short label shown in the picker.
  - `instructions`: the markdown body that becomes the system
    prompt the agent must follow for the turn.
  - `created_at` / `updated_at`: standard audit columns.

Idempotent: the inspector check skips the create when the table
already exists, mirroring the pattern from migration `0009`.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return name in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "user_skills"):
        return
    op.create_table(
        "user_skills",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("app_id", sa.String(128), nullable=False, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(300), nullable=False, server_default=""),
        sa.Column("instructions", sa.Text, nullable=False),
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
    # Composite index: the hot path is "list this user's skills for
    # this app". Same shape as `user_snippets` from 0009.
    op.create_index(
        "ix_user_skills_user_app",
        "user_skills",
        ["user_id", "app_id"],
    )
    # `(user_id, app_id, name)` unique so `/use_skill <name>`
    # resolves unambiguously per user/app pair.
    op.create_index(
        "ux_user_skills_user_app_name",
        "user_skills",
        ["user_id", "app_id", "name"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "user_skills"):
        return
    op.drop_index("ux_user_skills_user_app_name", table_name="user_skills")
    op.drop_index("ix_user_skills_user_app", table_name="user_skills")
    op.drop_table("user_skills")
