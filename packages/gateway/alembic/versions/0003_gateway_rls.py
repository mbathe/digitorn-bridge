"""Sprint H follow-up - apply RLS to gateway tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-05

The daemon's Sprint H (revision 0007 in alembic_version_daemon)
declared RLS on every user-owned table including the gateway ones,
but at that point gateway sprint E hadn't run yet so the gateway
tables didn't exist. The Sprint H migration's ``IF EXISTS`` guard
silently skipped them.

This migration closes the gap: same policy template, same helper
functions (``digitorn_current_user_id``, ``digitorn_is_admin``)
created by daemon Sprint H, applied to the 5 gateway tables.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


GATEWAY_RLS_TABLES: tuple[tuple[str, str], ...] = (
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

    for table, user_col in GATEWAY_RLS_TABLES:
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

    for table, _user_col in GATEWAY_RLS_TABLES:
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
