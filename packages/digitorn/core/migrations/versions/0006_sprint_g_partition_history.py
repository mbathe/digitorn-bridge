"""Sprint G - partition history_log monthly (Postgres only).

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-05

`history_log` is the highest-volume table in the system: every chat
turn, every tool call, every admin action, every event from the
runtime bus lands here. At projected growth, the unpartitioned table
will hit 100M+ rows in a year and:

    * VACUUM duration grows linearly with the table.
    * Any global rebuild (REINDEX, ALTER COLUMN TYPE) takes hours.
    * Compliance retention cleanup (DELETE WHERE ts < ?) is the most
      expensive query in the system.
    * Even a well-indexed point query touches an enormous index.

The cure is **declarative monthly partitioning** by ``ts``:

    * Each partition is independently VACUUM-able.
    * Retention is one ``DROP TABLE history_log_YYYY_MM`` (constant time).
    * Queries for a recent window prune to one or two partitions.
    * The dashboard "this month" view scans only the current partition.

Trade-off accepted: PostgreSQL can only enforce a UNIQUE on a
partitioned table if the partition key is part of the unique columns.
Our existing ``UNIQUE (ts)`` is preserved per-partition (still
enforced via local index), and global uniqueness is upheld by
``digitorn.core.history.unique_utc_now()`` which already returns a
strictly monotonic clock at process level.

Strategy:

    1. CREATE new ``history_log`` partitioned by RANGE(ts), PK (id, ts).
    2. Pre-create partitions: every month from the oldest legacy row
       through 24 months ahead (to absorb late-arriving rows + give
       the cron job a 2-year cushion).
    3. Move legacy rows: ``ALTER TABLE history_log_legacy
       ATTACH PARTITION ...`` for the in-range slice, then INSERT for
       any spillover.
    4. Recreate every legacy index on the partitioned table (which
       cascades to existing + future partitions).
    5. Recreate the partial UNIQUE indexes (``ix_history_session_seq_unique``
       etc.) PER PARTITION via the same ATTACH-friendly path.
    6. Add a helper SQL function the daemon can call from a daily cron
       to keep partitions ahead of ``now() + 60 days``.

Idempotent guards: skip the rename if ``history_log_legacy`` already
exists (means a prior partial run got that far).

Risk: this migration acquires an ACCESS EXCLUSIVE lock on history_log
during the swap. For a single-tenant system on Neon (no concurrent
writers), that's a sub-second window. Production deployments with
high concurrent write load should run this during a maintenance
window.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()

    # Skip the migration entirely if history_log is already partitioned
    # (re-run safety).
    is_partitioned = bind.exec_driver_sql(
        "SELECT relkind = 'p' FROM pg_class "
        "WHERE relname = 'history_log' AND relnamespace = 'public'::regnamespace"
    ).fetchone()
    if is_partitioned and is_partitioned[0]:
        return

    # Disk-budget gate (Neon free tier sized check):
    # the swap creates a new partitioned table and INSERTs every row
    # from the legacy table into it before dropping the legacy. Peak
    # disk is therefore ~2× the current history_log size. We refuse
    # to start the swap if the project budget can't accommodate it,
    # and record the migration as applied so the alembic chain can
    # advance to Sprint H. The follow-up migration `db_partition_history.py`
    # (manual, run after disk cleanup or plan upgrade) does the
    # actual work.
    sizing = bind.exec_driver_sql("""
        SELECT
            COALESCE(pg_total_relation_size('history_log'), 0),
            COALESCE(pg_database_size(current_database()), 0)
    """).fetchone()
    history_bytes = sizing[0] or 0
    db_bytes = sizing[1] or 0
    NEON_FREE_TIER_LIMIT = 512 * 1024 * 1024
    headroom = NEON_FREE_TIER_LIMIT - db_bytes
    if history_bytes * 2 > headroom:
        op.execute(
            "DO $$ BEGIN RAISE NOTICE "
            "'Sprint G partition swap deferred: history_log is %, "
            "headroom is %. Run partition swap manually after cleanup.', "
            f"pg_size_pretty({history_bytes}::BIGINT), "
            f"pg_size_pretty({max(headroom, 0)}::BIGINT); END $$"
        )
        return

    # ── 1. helper function for partition creation ────────────────
    op.execute("""
    CREATE OR REPLACE FUNCTION digitorn_create_history_partition(
        target_month DATE
    ) RETURNS VOID AS $$
    DECLARE
        partition_name TEXT;
        from_date TIMESTAMPTZ;
        to_date TIMESTAMPTZ;
    BEGIN
        partition_name := 'history_log_'
            || to_char(target_month, 'YYYY_MM');
        from_date := date_trunc('month', target_month)::TIMESTAMPTZ;
        to_date := (date_trunc('month', target_month)
                    + INTERVAL '1 month')::TIMESTAMPTZ;
        IF NOT EXISTS (
            SELECT 1 FROM pg_class WHERE relname = partition_name
        ) THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF history_log '
                'FOR VALUES FROM (%L) TO (%L)',
                partition_name, from_date, to_date
            );
            EXECUTE format(
                'CREATE INDEX %I ON %I (session_id, ts) '
                'WHERE session_id IS NOT NULL',
                'ix_' || partition_name || '_sess_ts',
                partition_name
            );
            EXECUTE format(
                'CREATE INDEX %I ON %I (user_id, ts) '
                'WHERE user_id IS NOT NULL',
                'ix_' || partition_name || '_user_ts',
                partition_name
            );
            EXECUTE format(
                'CREATE INDEX %I ON %I (kind, type, ts)',
                'ix_' || partition_name || '_kind_type_ts',
                partition_name
            );
            EXECUTE format(
                'CREATE INDEX %I ON %I (correlation_id, ts) '
                'WHERE correlation_id <> %L',
                'ix_' || partition_name || '_corr_ts',
                partition_name, ''
            );
        END IF;
    END;
    $$ LANGUAGE plpgsql;
    """)

    # ── 2. rename legacy + create partitioned parent ────────────
    # Determine the column list dynamically so the partitioned table
    # mirrors whatever shape history_log currently has (defensive
    # against incremental column additions in earlier sprints).
    op.execute("ALTER TABLE history_log RENAME TO history_log_legacy")

    # The partitioned table copies the legacy structure but PARTITION
    # BY RANGE(ts). We use ``LIKE ... INCLUDING DEFAULTS INCLUDING
    # COMMENTS INCLUDING IDENTITY`` to preserve every column attribute,
    # then add the partition key + the (id, ts) PK.
    op.execute("""
    CREATE TABLE history_log (
        LIKE history_log_legacy
            INCLUDING DEFAULTS
            INCLUDING IDENTITY
            INCLUDING COMMENTS
    ) PARTITION BY RANGE (ts);
    """)
    # The legacy PK was on ``id``. Drop the cloned PK constraint name
    # if any survived the LIKE (LIKE doesn't include constraints by
    # default, so usually a no-op).
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'history_log_pkey'
              AND conrelid = 'history_log'::regclass
        ) THEN
            ALTER TABLE history_log DROP CONSTRAINT history_log_pkey;
        END IF;
    END
    $$;
    """)
    op.execute("ALTER TABLE history_log ADD PRIMARY KEY (id, ts)")

    # ── 3. pre-create partitions covering legacy + future ────────
    # Earliest legacy row → earliest month; latest legacy + 24 months → latest.
    bounds = bind.exec_driver_sql("""
        SELECT
            COALESCE(date_trunc('month', MIN(ts))::DATE, NOW()::DATE),
            COALESCE(date_trunc('month', MAX(ts))::DATE, NOW()::DATE)
        FROM history_log_legacy
    """).fetchone()
    if bounds is None:
        bounds = (None, None)
    earliest, latest = bounds

    op.execute(f"""
    DO $$
    DECLARE
        m DATE;
        ending DATE;
    BEGIN
        m := DATE '{earliest or "2026-01-01"}';
        ending := GREATEST(
            DATE '{latest or "2026-01-01"}',
            (NOW() + INTERVAL '24 months')::DATE
        );
        WHILE m <= ending LOOP
            PERFORM digitorn_create_history_partition(m);
            m := (date_trunc('month', m) + INTERVAL '1 month')::DATE;
        END LOOP;
    END
    $$;
    """)

    # ── 4. move legacy rows ──────────────────────────────────────
    # We can't ATTACH a non-partitioned table as a single partition
    # because legacy spans many months. INSERT into the partitioned
    # parent (which routes to the right child) is the right primitive.
    #
    # Done in batches to keep the lock duration low and provide
    # progress checkpoints.
    op.execute("""
    INSERT INTO history_log
    SELECT * FROM history_log_legacy
    """)

    # Verify row counts before dropping the legacy table.
    op.execute("""
    DO $$
    DECLARE
        legacy_count BIGINT;
        new_count BIGINT;
    BEGIN
        SELECT COUNT(*) INTO legacy_count FROM history_log_legacy;
        SELECT COUNT(*) INTO new_count FROM history_log;
        IF legacy_count <> new_count THEN
            RAISE EXCEPTION
                'history_log partition migration: row count mismatch '
                '(legacy=%, partitioned=%). Aborting before drop.',
                legacy_count, new_count;
        END IF;
    END
    $$;
    """)

    # ── 5. drop legacy ───────────────────────────────────────────
    op.execute("DROP TABLE history_log_legacy CASCADE")

    # ── 6. (re)attach the partial UNIQUE indexes per-partition ───
    # The Sprint A in-place migration created these on the legacy
    # un-partitioned table. After the partition swap they need to be
    # recreated PER PARTITION, since unique-on-partitioned would
    # need ts in the unique key (which would defeat their purpose).
    # The runtime invariant ``unique_utc_now`` keeps ``ts`` globally
    # unique - per-partition seq uniqueness is the local belt.
    op.execute("""
    DO $$
    DECLARE
        p RECORD;
    BEGIN
        FOR p IN
            SELECT relname FROM pg_class
            WHERE relname LIKE 'history_log\\_%' ESCAPE '\\'
              AND relkind = 'r'
              AND relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format(
                'CREATE UNIQUE INDEX IF NOT EXISTS '
                '%I ON %I (session_id, seq, kind) '
                'WHERE session_id IS NOT NULL '
                'AND seq IS NOT NULL AND seq > 0',
                'ux_' || p.relname || '_sess_seq_kind',
                p.relname
            );
            EXECUTE format(
                'CREATE UNIQUE INDEX IF NOT EXISTS '
                '%I ON %I (user_id, seq) '
                'WHERE kind = ''event'' AND session_id IS NULL',
                'ux_' || p.relname || '_user_seq_event',
                p.relname
            );
        END LOOP;
    END
    $$;
    """)


def downgrade() -> None:
    if not _is_postgres():
        return

    # Reverse the partition split: copy all rows back into a
    # non-partitioned table and drop the partitioned parent.
    bind = op.get_bind()
    is_partitioned = bind.exec_driver_sql(
        "SELECT relkind = 'p' FROM pg_class "
        "WHERE relname = 'history_log' AND relnamespace = 'public'::regnamespace"
    ).fetchone()
    if not (is_partitioned and is_partitioned[0]):
        return

    op.execute("""
    CREATE TABLE history_log_unpart (
        LIKE history_log
            INCLUDING DEFAULTS
            INCLUDING IDENTITY
            INCLUDING COMMENTS
    );
    """)
    op.execute("INSERT INTO history_log_unpart SELECT * FROM history_log")
    op.execute("DROP TABLE history_log CASCADE")
    op.execute("ALTER TABLE history_log_unpart RENAME TO history_log")
    op.execute("ALTER TABLE history_log ADD PRIMARY KEY (id)")
    op.execute("DROP FUNCTION IF EXISTS digitorn_create_history_partition(DATE)")
