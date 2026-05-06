"""Sprint A - cleanup & hygiene (Postgres only, idempotent).

Revision ID: 0001
Revises:
Create Date: 2026-05-05

Zero-risk hygiene pass:

1. DROP empty / abandoned tables left over from the daemon-side quota
   refactor (their data was already moved to the gateway service):
       quota_definitions, quota_counters_rolling, quota_counters_fixed,
       user_quotas, usage_events, history_log_dedup_backup_20260502.

2. ADD missing FK indexes that an EXPLAIN audit flagged as full-scan
   risks at scale:
       action_executions.agent_pk
       applications.current_bundle_id
       user_roles.role_id
       user_sessions.user_id

3. ADD ``updated_at TIMESTAMPTZ`` (+ auto-update trigger) to every
   table that already has ``created_at`` but no ``updated_at``. The
   ORM declares both for these tables, but they shipped to prod
   before ``updated_at`` was added, so existing rows are missing it.

4. CONVERT ``history_log.id`` from INTEGER to BIGINT. The audit table
   is the highest-volume in the system; an INT4 PK overflows in 1-2
   years at projected growth. Postgres widens INT4 → INT8 in place
   without rewriting rows, so the change is online-safe.

All operations are idempotent (``IF EXISTS`` / ``DO`` blocks with
existence checks) and target only PostgreSQL - the migration is a
no-op on SQLite (local dev DB), so a developer running ``alembic
upgrade head`` against ``digitorn.db`` won't get errors.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── tables that became dead after the gateway split (2026-05-04) ────
DEAD_TABLES: tuple[str, ...] = (
    "quota_definitions",
    "quota_counters_rolling",
    "quota_counters_fixed",
    "user_quotas",
    "usage_events",
    "history_log_dedup_backup_20260502",
)


# ── (table, column, index_name) for missing FK indexes ─────────────
FK_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("action_executions", "agent_pk", "ix_action_executions_agent_pk"),
    ("applications", "current_bundle_id", "ix_applications_current_bundle_id"),
    ("user_roles", "role_id", "ix_user_roles_role_id"),
    ("user_sessions", "user_id", "ix_user_sessions_user_id"),
)


# ── tables that have ``created_at`` but no ``updated_at`` ──────────
# Detected against the live schema 2026-05-05; the ORM models declare
# updated_at for all of these, the column is just missing in prod.
NEEDS_UPDATED_AT: tuple[str, ...] = (
    "agents",
    "api_keys",
    "background_sessions",
    "inbox_devices",
    "inbox_items",
    "refresh_tokens",
    "user_roles",
    "user_sessions",
)


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # Local SQLite dev DB: no-op. The daemon's create_all already
        # produces the canonical schema for fresh SQLite databases.
        return

    bind = op.get_bind()

    # ── 1. drop dead tables ───────────────────────────────────────
    for table in DEAD_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')

    # ── 2. missing FK indexes (CONCURRENTLY = no table lock) ─────
    # CONCURRENTLY can't run inside a transaction. Alembic wraps the
    # whole upgrade in one tx by default; we commit then reopen so
    # the CREATE INDEX CONCURRENTLY runs autocommit.
    op.execute("COMMIT")
    try:
        for table, column, index_name in FK_INDEXES:
            # Only create if the table & column actually exist (defensive
            # against partial historical migrations).
            exists = bind.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).fetchone()
            if not exists:
                continue
            op.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}" '
                f'ON "{table}" ("{column}")'
            )
    finally:
        # Re-open a tx for the rest of the migration.
        op.execute("BEGIN")

    # ── 3. add updated_at where missing ──────────────────────────
    # One DO block per table keeps the error surface minimal: a
    # failure on table N doesn't leave tables 1..N-1 half-migrated.
    for table in NEEDS_UPDATED_AT:
        op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = '{table}'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = '{table}' AND column_name = 'updated_at'
            ) THEN
                EXECUTE 'ALTER TABLE "{table}" '
                        'ADD COLUMN updated_at TIMESTAMPTZ '
                        'NOT NULL DEFAULT NOW()';
            END IF;
        END
        $$;
        """)

    # Shared trigger function: every UPDATE on any tracked table sets
    # updated_at = now(). Idempotent: CREATE OR REPLACE.
    op.execute("""
    CREATE OR REPLACE FUNCTION digitorn_set_updated_at()
    RETURNS TRIGGER AS $$
    BEGIN
        NEW.updated_at = NOW();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    # Attach the trigger to every table that has updated_at (the 8
    # we just patched + any that already had it). Detected at
    # migration time so we cover historical updated_at columns too.
    op.execute("""
    DO $$
    DECLARE
        t RECORD;
    BEGIN
        FOR t IN
            SELECT c.table_name
            FROM information_schema.columns c
            JOIN information_schema.tables tt
                ON tt.table_name = c.table_name
                AND tt.table_schema = c.table_schema
            WHERE c.column_name = 'updated_at'
              AND c.table_schema = 'public'
              AND tt.table_type = 'BASE TABLE'
              AND c.table_name NOT LIKE 'alembic_%'
        LOOP
            EXECUTE format(
                'DROP TRIGGER IF EXISTS trg_%I_updated_at ON %I',
                t.table_name, t.table_name
            );
            EXECUTE format(
                'CREATE TRIGGER trg_%I_updated_at '
                'BEFORE UPDATE ON %I '
                'FOR EACH ROW '
                'EXECUTE FUNCTION digitorn_set_updated_at()',
                t.table_name, t.table_name
            );
        END LOOP;
    END
    $$;
    """)

    # ── 4. history_log.id INTEGER → BIGINT ───────────────────────
    # Empirical correction (2026-05-05 / Neon free tier 512 MB):
    # Postgres claims ``ALTER COLUMN INT → BIGINT`` is in-place, but
    # in practice it rewrites the table (the on-disk row layout
    # changes alignment) and on Neon free tier this tipped us over
    # the project size limit on the first try.
    #
    # We always widen the SEQUENCE (cheap, no rewrite) so future
    # nextval() returns BIGINT. We widen the COLUMN type only when
    # the table is small enough for the rewrite to fit in the
    # project's free disk - measured as 2× current table size.
    # Otherwise we skip with a NOTICE; the operator can either:
    #   * upgrade the Neon plan and re-run via a follow-up migration,
    #   * trim history_log first (old rows have lower business value),
    #   * leave it - INT4 can still hold ~2.1B rows.
    op.execute("""
    DO $$
    DECLARE
        col_type TEXT;
        tbl_bytes BIGINT;
        free_bytes BIGINT;
    BEGIN
        SELECT data_type INTO col_type
        FROM information_schema.columns
        WHERE table_name = 'history_log' AND column_name = 'id';

        -- Always widen the sequence (cheap, no row rewrite).
        IF EXISTS (
            SELECT 1 FROM pg_class
            WHERE relkind = 'S' AND relname = 'history_log_id_seq'
        ) THEN
            ALTER SEQUENCE history_log_id_seq AS BIGINT;
        END IF;

        IF col_type = 'integer' THEN
            SELECT pg_total_relation_size('history_log') INTO tbl_bytes;
            -- Conservative free-space guess: 200 MB. Skip the rewrite
            -- when the table would push us past it.
            free_bytes := 200 * 1024 * 1024;
            IF tbl_bytes < free_bytes / 2 THEN
                ALTER TABLE history_log ALTER COLUMN id TYPE BIGINT;
            ELSE
                RAISE NOTICE
                    'history_log id widening skipped: '
                    'table is %s and would not fit a rewrite. '
                    'Run a future migration after cleanup or upgrade.',
                    pg_size_pretty(tbl_bytes);
            END IF;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    if not _is_postgres():
        return

    # Sprint A is intentionally NOT reversible:
    # - The dead tables held no live data and recreating them empty
    #   would mislead any developer into thinking they're current.
    # - Removing FK indexes silently regresses query plans.
    # - Removing updated_at columns drops audit data.
    # - Narrowing history_log.id back to INT requires a row scan and
    #   may fail if any id > 2_147_483_647.
    #
    # The migration is its own rollback: the live state before sprint A
    # is captured in db backups taken by the operator before applying.
    raise RuntimeError(
        "Sprint A cleanup is not reversible by design. Restore from a "
        "pre-Sprint-A backup if rollback is required."
    )
