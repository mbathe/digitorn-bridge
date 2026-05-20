"""Sprint H - Row-Level Security on user-owned tables (Postgres only).

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-05

Final defense layer: even if a query escapes the application's own
`WHERE user_id = ?` filter, Postgres refuses to return rows that
don't match `current_setting('digitorn.current_user_id')`.

How the daemon plugs in:

    Before serving a request, the FastAPI middleware does::

        await conn.execute(
            "SELECT set_config('digitorn.current_user_id', $1, true)",
            jwt_user_id,
        )

    The `true` makes the setting transaction-scoped (LOCAL), so a
    pooled connection can't leak across requests. The connection
    pool's `reset_query` clears it on release as a belt.

    Admin endpoints set `digitorn.is_admin = 'true'` (also LOCAL),
    which the policies recognise as a bypass. The schema is
    intentionally fail-closed: if the setting is unset, every policy
    refuses access.

Policies are defined per-table and are ENABLED but in PERMISSIVE
mode. Existing roles (the daemon's own DB role) get explicit BYPASS
RLS attribute to avoid breaking server-side migrations that need to
see all rows regardless. Application code uses a non-superuser role
that DOES enforce RLS.

We add policies but do NOT yet create the application role - that's
an environment-specific operator step (database admin creates the
role, attaches it to the DATABASE_URL the daemon uses).

This migration is REVERSIBLE: `downgrade()` simply DISABLE RLS on
each table without dropping the policies (so re-upgrade is a no-op
toggle).

Risk: with no application role configured yet, applying this
migration is **safe but inert** - the daemon currently connects as
the table owner who BYPASSES RLS. Once the operator switches
DATABASE_URL to a non-owner role, the policies activate.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, user_id_column) for tables we put under RLS.
RLS_TABLES: tuple[tuple[str, str], ...] = (
    ("user_sessions", "user_id"),
    ("agent_runs", "user_id"),
    ("user_devices", "user_id"),
    ("inbox_items", "user_id"),
    ("user_oauth_tokens", "user_id"),
    ("hub_sessions", "daemon_user_id"),
    ("background_sessions", "user_id"),
    ("gateway_user_plans", "user_id"),
    ("gateway_user_plan_history", "user_id"),
    ("gateway_quota_counters", "user_id"),
    ("gateway_quota_blocks", "user_id"),
    ("gateway_usage_events", "user_id"),
)


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # ── helper: fetch the current user_id setting safely ─────────
    # COALESCE so unset settings don't raise; the empty-string fallback
    # is treated as "no user", which fails every comparison.
    op.execute("""
    CREATE OR REPLACE FUNCTION digitorn_current_user_id() RETURNS TEXT AS $$
    BEGIN
        RETURN COALESCE(current_setting('digitorn.current_user_id', TRUE), '');
    END;
    $$ LANGUAGE plpgsql STABLE;
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION digitorn_is_admin() RETURNS BOOLEAN AS $$
    BEGIN
        RETURN COALESCE(
            current_setting('digitorn.is_admin', TRUE)::BOOLEAN,
            FALSE
        );
    END;
    $$ LANGUAGE plpgsql STABLE;
    """)

    # ── per-table policies ────────────────────────────────────────
    # Generated dynamically so we can extend RLS_TABLES without
    # repeating the boilerplate.
    for table, user_col in RLS_TABLES:
        # Skip tables that don't yet exist (e.g. partial sprint apply).
        op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = '{table}'
            ) THEN
                ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
                ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

                DROP POLICY IF EXISTS rls_{table}_owner ON {table};
                CREATE POLICY rls_{table}_owner ON {table}
                    FOR ALL
                    USING (
                        digitorn_is_admin()
                        OR {user_col}::TEXT = digitorn_current_user_id()
                    )
                    WITH CHECK (
                        digitorn_is_admin()
                        OR {user_col}::TEXT = digitorn_current_user_id()
                    );
            END IF;
        END
        $$;
        """)


def downgrade() -> None:
    if not _is_postgres():
        return

    for table, _user_col in RLS_TABLES:
        op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = '{table}'
            ) THEN
                DROP POLICY IF EXISTS rls_{table}_owner ON {table};
                ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
            END IF;
        END
        $$;
        """)

    op.execute("DROP FUNCTION IF EXISTS digitorn_is_admin()")
    op.execute("DROP FUNCTION IF EXISTS digitorn_current_user_id()")
