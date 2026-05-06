"""Sprint D - user_sessions refactor (Postgres only).

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-05

Three changes to `user_sessions`:

1. ADD ``status`` (active|completed|archived|deleted),
       ``title`` (display name shown in the chat sidebar),
       ``deleted_at`` (soft delete timestamp).

2. RENAME ``session_id`` → ``external_sid``. The column was always a
   client-supplied opaque key, never the table's PK. Calling it
   ``session_id`` was confusing because every other table calls the
   internal PK ``id`` and the external one ``external_sid``. The
   composite unique index ``(app_id, session_id)`` is renamed
   accordingly. Application code (``UserSession.session_id``) keeps
   the Python attribute name; the column it maps to is updated in
   models.py in the same change.

3. ADD ``last_completed_run_id`` FK on agent_runs (set by the
   runtime after each run completion to support "resume last run"
   in the dashboard without scanning agent_runs).

Idempotent: every step guarded by IF NOT EXISTS / EXISTS check.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # ── 1. add status / title / deleted_at ────────────────────────
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user_sessions' AND column_name = 'status'
        ) THEN
            ALTER TABLE user_sessions
              ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'active';
            COMMENT ON COLUMN user_sessions.status IS
                'active | completed | archived | deleted';
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user_sessions' AND column_name = 'title'
        ) THEN
            ALTER TABLE user_sessions
              ADD COLUMN title VARCHAR(512) NULL;
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user_sessions' AND column_name = 'deleted_at'
        ) THEN
            ALTER TABLE user_sessions
              ADD COLUMN deleted_at TIMESTAMPTZ NULL;
        END IF;
    END
    $$;
    """)

    # Index on (user_id, deleted_at IS NULL, last_active_at) for the
    # default sidebar query: "my sessions, not deleted, by recency".
    op.execute("COMMIT")
    try:
        op.execute("""
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            ix_user_sessions_user_active
        ON user_sessions (user_id, last_active_at DESC)
        WHERE deleted_at IS NULL
        """)
        op.execute("""
        CREATE INDEX CONCURRENTLY IF NOT EXISTS
            ix_user_sessions_status
        ON user_sessions (status)
        WHERE deleted_at IS NULL
        """)
    finally:
        op.execute("BEGIN")

    # ── 2. rename session_id → external_sid ───────────────────────
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user_sessions' AND column_name = 'session_id'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user_sessions' AND column_name = 'external_sid'
        ) THEN
            ALTER TABLE user_sessions RENAME COLUMN session_id TO external_sid;
            ALTER INDEX IF EXISTS ix_user_sessions_session_id
                RENAME TO ix_user_sessions_external_sid;
            ALTER INDEX IF EXISTS ix_user_sessions_app_session
                RENAME TO ix_user_sessions_app_external_sid;
        END IF;
    END
    $$;
    """)

    # ── 3. last_completed_run_id FK on agent_runs ─────────────────
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user_sessions'
              AND column_name = 'last_completed_run_id'
        ) THEN
            ALTER TABLE user_sessions
              ADD COLUMN last_completed_run_id VARCHAR(64) NULL
              REFERENCES agent_runs(id) ON DELETE SET NULL;
        END IF;
    END
    $$;
    """)
    op.execute("COMMIT")
    try:
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_user_sessions_last_completed_run "
            "ON user_sessions (last_completed_run_id)"
        )
    finally:
        op.execute("BEGIN")


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_user_sessions_last_completed_run"
    )
    op.execute(
        "ALTER TABLE user_sessions DROP COLUMN IF EXISTS last_completed_run_id"
    )

    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user_sessions' AND column_name = 'external_sid'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user_sessions' AND column_name = 'session_id'
        ) THEN
            ALTER TABLE user_sessions RENAME COLUMN external_sid TO session_id;
            ALTER INDEX IF EXISTS ix_user_sessions_external_sid
                RENAME TO ix_user_sessions_session_id;
            ALTER INDEX IF EXISTS ix_user_sessions_app_external_sid
                RENAME TO ix_user_sessions_app_session;
        END IF;
    END
    $$;
    """)

    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_user_sessions_status"
    )
    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_user_sessions_user_active"
    )
    op.execute("ALTER TABLE user_sessions DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE user_sessions DROP COLUMN IF EXISTS title")
    op.execute("ALTER TABLE user_sessions DROP COLUMN IF EXISTS status")
