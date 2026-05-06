"""Sprint E - full gateway schema (Postgres only, idempotent).

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-05

Extends the initial gateway tables with the full v2 design and adds
the partitioned usage events table.

Changes vs initial schema:

1. ``gateway_plans``: pricing fields (monthly_price_cents, currency,
   stripe_price_id), display fields (icon, color, badge), ordering
   (sort_order), soft delete (archived_at).
2. ``gateway_user_plans``: stripe_subscription_id, period (start, end,
   trial_end), cancel_at_period_end, override_reason.
3. NEW ``gateway_user_plan_history``: append-only audit of every plan
   change. Retention is forever; gateway never deletes a row.
4. ``gateway_quota_blocks``: triggered_by_run_id (soft FK to
   agent_runs.id - cross-service so no DB-level FK), notify_after_at.
5. NEW ``gateway_usage_events`` partitioned monthly. Each turn /
   tool call writes one row. Cost breakdown stored as JSONB.

Idempotent: every CREATE TABLE / ALTER COLUMN guarded; safe to run
twice. Drops nothing - upgrade-only path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # ── 1. gateway_plans extensions ───────────────────────────────
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'gateway_plans' AND column_name = 'monthly_price_cents'
        ) THEN
            ALTER TABLE gateway_plans
              ADD COLUMN monthly_price_cents BIGINT NOT NULL DEFAULT 0,
              ADD COLUMN annual_price_cents BIGINT NOT NULL DEFAULT 0,
              ADD COLUMN currency VARCHAR(3) NOT NULL DEFAULT 'USD',
              ADD COLUMN stripe_price_id_monthly VARCHAR(128) NULL,
              ADD COLUMN stripe_price_id_annual VARCHAR(128) NULL,
              ADD COLUMN icon VARCHAR(64) NULL,
              ADD COLUMN color VARCHAR(16) NULL,
              ADD COLUMN badge VARCHAR(32) NULL,
              ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 100,
              ADD COLUMN archived_at TIMESTAMPTZ NULL,
              ADD COLUMN visibility VARCHAR(16) NOT NULL DEFAULT 'public';
            COMMENT ON COLUMN gateway_plans.visibility IS
                'public | hidden | beta - controls plan picker exposure';
        END IF;
    END
    $$;
    """)

    # JSON → JSONB for quota_def + override_quota_def (the gateway
    # initial migration declared sa.JSON, which maps to ``json`` on
    # Postgres - swap to jsonb for indexing + containment ops).
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'gateway_plans'
              AND column_name = 'quota_def'
              AND data_type = 'json'
        ) THEN
            ALTER TABLE gateway_plans
              ALTER COLUMN quota_def TYPE JSONB USING quota_def::jsonb;
            ALTER TABLE gateway_plans
              ALTER COLUMN quota_def SET DEFAULT '{}'::jsonb;
        END IF;
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'gateway_user_plans'
              AND column_name = 'override_quota_def'
              AND data_type = 'json'
        ) THEN
            ALTER TABLE gateway_user_plans
              ALTER COLUMN override_quota_def TYPE JSONB
              USING override_quota_def::jsonb;
        END IF;
    END
    $$;
    """)

    # ── 2. gateway_user_plans extensions ─────────────────────────
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'gateway_user_plans'
              AND column_name = 'stripe_subscription_id'
        ) THEN
            ALTER TABLE gateway_user_plans
              ADD COLUMN stripe_subscription_id VARCHAR(128) NULL,
              ADD COLUMN period_start_at TIMESTAMPTZ NULL,
              ADD COLUMN period_end_at TIMESTAMPTZ NULL,
              ADD COLUMN trial_end_at TIMESTAMPTZ NULL,
              ADD COLUMN cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
              ADD COLUMN cancelled_at TIMESTAMPTZ NULL,
              ADD COLUMN override_reason VARCHAR(256) NULL,
              ADD COLUMN assigned_by VARCHAR(64) NULL;
            COMMENT ON COLUMN gateway_user_plans.assigned_by IS
                'User id of the admin who assigned this plan (null = self-service / system)';
        END IF;
    END
    $$;
    """)

    # ── 3. gateway_user_plan_history ──────────────────────────────
    op.create_table(
        "gateway_user_plan_history",
        sa.Column(
            "id",
            sa.BigInteger,
            sa.Identity(start=1, cycle=False),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "from_plan_id",
            sa.String(64),
            sa.ForeignKey("gateway_plans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "to_plan_id",
            sa.String(64),
            sa.ForeignKey("gateway_plans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "changed_by",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            comment="Who triggered the change. NULL = system / scheduled job.",
        ),
        sa.Column(
            "change_kind",
            sa.String(16),
            nullable=False,
            comment="upgrade | downgrade | initial | cancel | reactivate | admin",
        ),
        sa.Column("reason", sa.String(512), nullable=True),
        sa.Column(
            "stripe_event_id",
            sa.String(128),
            nullable=True,
            comment="Stripe webhook event id when triggered by billing.",
        ),
        sa.Column(
            "snapshot",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Full quota_def snapshot of the plan at the moment of change.",
        ),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )
    op.create_index(
        "ix_gateway_user_plan_history_user_changed",
        "gateway_user_plan_history",
        ["user_id", "changed_at"],
    )

    # ── 4. gateway_quota_blocks extensions ───────────────────────
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'gateway_quota_blocks'
              AND column_name = 'triggered_by_run_id'
        ) THEN
            ALTER TABLE gateway_quota_blocks
              ADD COLUMN triggered_by_run_id VARCHAR(64) NULL,
              ADD COLUMN notify_after_at TIMESTAMPTZ NULL,
              ADD COLUMN cleared_at TIMESTAMPTZ NULL,
              ADD COLUMN cleared_by VARCHAR(64) NULL;
            COMMENT ON COLUMN gateway_quota_blocks.triggered_by_run_id IS
                'agent_runs.id at the time the block fired (soft cross-service ref, no FK)';
        END IF;
    END
    $$;
    """)

    # ── 5. gateway_usage_events (partitioned monthly) ────────────
    # Parent (declarative-partitioned) - rows MUST land in a partition.
    op.execute("""
    CREATE TABLE IF NOT EXISTS gateway_usage_events (
        id BIGINT GENERATED ALWAYS AS IDENTITY (CYCLE),
        user_id VARCHAR(64) NOT NULL,
        run_id VARCHAR(64) NULL,
        app_id VARCHAR(255) NULL,
        external_sid VARCHAR(255) NULL,
        provider VARCHAR(64) NOT NULL,
        model VARCHAR(128) NOT NULL,
        kind VARCHAR(16) NOT NULL DEFAULT 'completion',
        prompt_tokens BIGINT NOT NULL DEFAULT 0,
        completion_tokens BIGINT NOT NULL DEFAULT 0,
        cache_read_tokens BIGINT NOT NULL DEFAULT 0,
        cache_write_tokens BIGINT NOT NULL DEFAULT 0,
        total_tokens BIGINT GENERATED ALWAYS AS (
            prompt_tokens + completion_tokens
            + cache_read_tokens + cache_write_tokens
        ) STORED,
        latency_ms BIGINT NULL,
        cost_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
        total_cost_usd NUMERIC(14, 6) NOT NULL DEFAULT 0,
        error_class VARCHAR(64) NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (id, created_at)
    ) PARTITION BY RANGE (created_at);
    """)

    # Trigger to materialise total_cost_usd from cost_breakdown.
    op.execute("""
    CREATE OR REPLACE FUNCTION gateway_usage_events_recompute_cost()
    RETURNS TRIGGER AS $$
    BEGIN
        NEW.total_cost_usd := COALESCE(
            (
                SELECT SUM((value->>'total_usd')::NUMERIC)
                FROM jsonb_each(NEW.cost_breakdown)
                WHERE jsonb_typeof(value) = 'object'
                  AND value ? 'total_usd'
            ),
            0
        );
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    # Helper that creates a monthly partition. Called by Python below
    # to seed current + next 12 months; runtime ops can call it later
    # to extend the rolling window.
    op.execute("""
    CREATE OR REPLACE FUNCTION gateway_create_usage_partition(
        target_month DATE
    ) RETURNS VOID AS $$
    DECLARE
        partition_name TEXT;
        from_date TIMESTAMPTZ;
        to_date TIMESTAMPTZ;
    BEGIN
        partition_name := 'gateway_usage_events_'
            || to_char(target_month, 'YYYY_MM');
        from_date := date_trunc('month', target_month)::TIMESTAMPTZ;
        to_date := (date_trunc('month', target_month)
                    + INTERVAL '1 month')::TIMESTAMPTZ;

        IF NOT EXISTS (
            SELECT 1 FROM pg_class WHERE relname = partition_name
        ) THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF gateway_usage_events '
                'FOR VALUES FROM (%L) TO (%L)',
                partition_name, from_date, to_date
            );
            EXECUTE format(
                'CREATE INDEX %I ON %I (user_id, created_at)',
                'ix_' || partition_name || '_user_created',
                partition_name
            );
            EXECUTE format(
                'CREATE INDEX %I ON %I (run_id) WHERE run_id IS NOT NULL',
                'ix_' || partition_name || '_run',
                partition_name
            );
        END IF;
    END;
    $$ LANGUAGE plpgsql;
    """)

    # Bind the trigger to the parent (propagates to every partition).
    op.execute("""
    CREATE TRIGGER trg_gateway_usage_events_recompute_cost
    BEFORE INSERT OR UPDATE OF cost_breakdown ON gateway_usage_events
    FOR EACH ROW EXECUTE FUNCTION gateway_usage_events_recompute_cost();
    """)

    # FK on user_id - declared on the parent so every partition inherits.
    op.execute("""
    ALTER TABLE gateway_usage_events
      ADD CONSTRAINT fk_gateway_usage_events_user
      FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
    """)

    # Seed partitions: current month + next 12.
    now = datetime.now(timezone.utc)
    for offset in range(0, 13):
        month = (
            now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        )
        # advance offset months
        m = month.month - 1 + offset
        target_year = month.year + m // 12
        target_month = (m % 12) + 1
        date_literal = f"{target_year:04d}-{target_month:02d}-01"
        op.execute(
            f"SELECT gateway_create_usage_partition(DATE '{date_literal}')"
        )

    # ── 6. dashboard support views ────────────────────────────────
    op.execute("""
    CREATE OR REPLACE VIEW v_user_quota_state AS
    SELECT
        u.id AS user_id,
        up.plan_id,
        p.name AS plan_name,
        COALESCE(up.override_quota_def, p.quota_def) AS effective_quota_def,
        b.blocked_until,
        b.reason AS block_reason,
        b.metric AS block_metric,
        (
            SELECT jsonb_object_agg(c.metric || ':' || c.window_key, c.value)
            FROM gateway_quota_counters c
            WHERE c.user_id = u.id
        ) AS counters
    FROM users u
    LEFT JOIN gateway_user_plans up ON up.user_id = u.id
    LEFT JOIN gateway_plans p ON p.id = up.plan_id
    LEFT JOIN gateway_quota_blocks b
        ON b.user_id = u.id AND b.blocked_until > NOW();
    """)

    op.execute("""
    CREATE OR REPLACE VIEW v_usage_top_users_month AS
    SELECT
        user_id,
        COUNT(*)                  AS event_count,
        SUM(total_tokens)::BIGINT AS tokens_month,
        SUM(total_cost_usd)       AS cost_month_usd,
        AVG(latency_ms)::BIGINT   AS avg_latency_ms,
        MAX(created_at)           AS last_event_at
    FROM gateway_usage_events
    WHERE created_at >= date_trunc('month', NOW())
    GROUP BY user_id
    ORDER BY cost_month_usd DESC
    LIMIT 50;
    """)


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute("DROP VIEW IF EXISTS v_usage_top_users_month")
    op.execute("DROP VIEW IF EXISTS v_user_quota_state")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_gateway_usage_events_recompute_cost "
        "ON gateway_usage_events"
    )
    op.execute("DROP FUNCTION IF EXISTS gateway_usage_events_recompute_cost")
    op.execute("DROP TABLE IF EXISTS gateway_usage_events CASCADE")
    op.execute("DROP FUNCTION IF EXISTS gateway_create_usage_partition(DATE)")

    op.execute(
        "ALTER TABLE gateway_quota_blocks "
        "DROP COLUMN IF EXISTS cleared_by, "
        "DROP COLUMN IF EXISTS cleared_at, "
        "DROP COLUMN IF EXISTS notify_after_at, "
        "DROP COLUMN IF EXISTS triggered_by_run_id"
    )

    op.drop_index(
        "ix_gateway_user_plan_history_user_changed",
        table_name="gateway_user_plan_history",
    )
    op.drop_table("gateway_user_plan_history")

    op.execute("""
    ALTER TABLE gateway_user_plans
      DROP COLUMN IF EXISTS assigned_by,
      DROP COLUMN IF EXISTS override_reason,
      DROP COLUMN IF EXISTS cancelled_at,
      DROP COLUMN IF EXISTS cancel_at_period_end,
      DROP COLUMN IF EXISTS trial_end_at,
      DROP COLUMN IF EXISTS period_end_at,
      DROP COLUMN IF EXISTS period_start_at,
      DROP COLUMN IF EXISTS stripe_subscription_id;
    """)

    op.execute("""
    ALTER TABLE gateway_plans
      DROP COLUMN IF EXISTS visibility,
      DROP COLUMN IF EXISTS archived_at,
      DROP COLUMN IF EXISTS sort_order,
      DROP COLUMN IF EXISTS badge,
      DROP COLUMN IF EXISTS color,
      DROP COLUMN IF EXISTS icon,
      DROP COLUMN IF EXISTS stripe_price_id_annual,
      DROP COLUMN IF EXISTS stripe_price_id_monthly,
      DROP COLUMN IF EXISTS currency,
      DROP COLUMN IF EXISTS annual_price_cents,
      DROP COLUMN IF EXISTS monthly_price_cents;
    """)
