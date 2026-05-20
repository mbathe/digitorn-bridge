"""Sprint B - JSONB conversion + GIN indexes (Postgres only).

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-05

Convert every `json` column in the public schema to `jsonb` and
attach GIN indexes to the columns the dashboard / runtime actually
search through.

Why JSONB:
    * Indexable (`json` is not).
    * Operator-rich: `->`, `->>`, `@>`, `?`, `#>`.
    * Smaller on disk (binary representation).
    * Read latency 5-20x lower at scale.

The cast `column::jsonb` is loss-less for valid JSON. Invalid rows
(e.g. a corrupt `{}` literal stored as a TEXT cast) would fail the
ALTER. We use `USING column::jsonb` so SQL parses each row; if a row
fails, the migration aborts cleanly and the operator can fix the
offending row before re-running.

GIN indexes use the `jsonb_path_ops` operator class on hot columns
that the runtime queries with `@>` containment, and the default
`jsonb_ops` on columns the dashboard searches by key existence.

Idempotent: ALTER ... TYPE JSONB is a no-op if the column is already
jsonb. CREATE INDEX uses IF NOT EXISTS.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── (table, column, opclass) for hot GIN indexes ───────────────────
# opclass jsonb_path_ops: smaller, faster for `@>` (containment),
#                         doesn't support key-existence (`?`).
# opclass jsonb_ops      : default, supports both `@>` and `?`.
GIN_INDEXES: tuple[tuple[str, str, str], ...] = (
    # Applications: tags array searched by membership.
    ("applications", "tags", "jsonb_path_ops"),
    # App module configs: per-app config blob searched by key.
    ("app_module_configs", "config", "jsonb_path_ops"),
    ("app_module_configs", "constraints", "jsonb_path_ops"),
    # Action executions: params / result searched for tracing.
    ("action_executions", "params", "jsonb_path_ops"),
    ("action_executions", "result", "jsonb_path_ops"),
    # Session checkpoints: snapshots searched for replay.
    ("session_checkpoints", "memory_snapshot", "jsonb_path_ops"),
    ("session_checkpoints", "workbench_snapshot", "jsonb_path_ops"),
    # Credentials display metadata + audit extra.
    ("credentials", "display_metadata", "jsonb_path_ops"),
    ("credential_audit", "extra", "jsonb_path_ops"),
    # Inbox: per-item metadata.
    ("inbox_items", "item_metadata", "jsonb_path_ops"),
    # History log payload / before / after - audit trace lookups.
    ("history_log", "payload", "jsonb_path_ops"),
    ("history_log", "before", "jsonb_path_ops"),
    ("history_log", "after", "jsonb_path_ops"),
    # Activations: trigger payload / params searched on event lookup.
    ("activations", "params", "jsonb_path_ops"),
    ("activation_events", "data", "jsonb_path_ops"),
)


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # JSON → JSONB across every `public` column; tables over 50 MB
    # are deferred (the ALTER rewrites the table, ~2× disk).
    op.execute("""
    DO $$
    DECLARE
        c RECORD;
        tbl_bytes BIGINT;
        threshold BIGINT := 50 * 1024 * 1024;  -- 50 MB
    BEGIN
        FOR c IN
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND data_type = 'json'
              AND table_name NOT LIKE 'alembic_%'
        LOOP
            SELECT pg_total_relation_size(c.table_name::regclass)
              INTO tbl_bytes;
            IF tbl_bytes < threshold THEN
                EXECUTE format(
                    'ALTER TABLE %I ALTER COLUMN %I TYPE JSONB USING %I::jsonb',
                    c.table_name, c.column_name, c.column_name
                );
            ELSE
                RAISE NOTICE
                    'JSONB conversion deferred for %.% (table is %s, '
                    'over the 50 MB safe-rewrite threshold).',
                    c.table_name, c.column_name, pg_size_pretty(tbl_bytes);
            END IF;
        END LOOP;
    END
    $$;
    """)

    # ── 2. GIN indexes on hot search paths ──────────────────────
    # CREATE INDEX CONCURRENTLY can't run inside a tx. Commit then
    # re-open after the loop.
    op.execute("COMMIT")
    bind = op.get_bind()
    try:
        for table, column, opclass in GIN_INDEXES:
            # Skip if column doesn't exist (defensive vs schema drift).
            exists = bind.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).fetchone()
            if not exists:
                continue
            # Confirm the column is now jsonb (the conversion above
            # may have skipped it if it was already a non-json type
            # like Text). GIN with jsonb_path_ops needs jsonb.
            if exists[0] != "jsonb":
                continue
            index_name = f"ix_{table}_{column}_gin"
            op.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}" '
                f'ON "{table}" USING GIN ("{column}" {opclass})'
            )
    finally:
        op.execute("BEGIN")

    # Pin DEFAULT expressions to `'{}'::jsonb` to avoid per-INSERT casts.
    op.execute("""
    DO $$
    DECLARE
        c RECORD;
    BEGIN
        FOR c IN
            SELECT table_name, column_name, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND data_type = 'jsonb'
              AND column_default IS NOT NULL
              AND column_default NOT LIKE '%::jsonb'
              AND column_default NOT LIKE '%::json'
        LOOP
            EXECUTE format(
                'ALTER TABLE %I ALTER COLUMN %I SET DEFAULT %s::jsonb',
                c.table_name, c.column_name, c.column_default
            );
        END LOOP;
    END
    $$;
    """)


def downgrade() -> None:
    if not _is_postgres():
        return

    # Drop GIN indexes first - JSONB → JSON cast invalidates them.
    op.execute("COMMIT")
    try:
        for table, column, _opclass in GIN_INDEXES:
            index_name = f"ix_{table}_{column}_gin"
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{index_name}"')
    finally:
        op.execute("BEGIN")

    # JSONB → JSON via the same dynamic loop.
    op.execute("""
    DO $$
    DECLARE
        c RECORD;
    BEGIN
        FOR c IN
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND data_type = 'jsonb'
              AND table_name NOT LIKE 'alembic_%'
        LOOP
            EXECUTE format(
                'ALTER TABLE %I ALTER COLUMN %I TYPE JSON USING %I::json',
                c.table_name, c.column_name, c.column_name
            );
        END LOOP;
    END
    $$;
    """)
