"""Sprint C - 4-level agent tracking refonte (Postgres only).

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-05

Introduces the canonical 4-level agent tracking hierarchy:

    user_sessions
      └── session_agents      (renamed from `agents`)
            └── agent_runs    (NEW - one row per spawn / turn-set)
                  ├── agent_run_events   (NEW - append-only timeline)
                  └── action_executions  (existing, FK now agent_run_id)

Why:
    The old `agents` table was a single flat record per (session, agent_id).
    There was no history of *individual runs*: you couldn't tell from the
    DB whether the agent was currently busy, how many turns it had used
    on the active run, what it had spent in tokens, etc. The dashboard
    couldn't surface "agents running now" or "top cost agents in 7 days"
    because the data simply wasn't recorded.

    `agent_runs` makes one row per launch (background or wait-for):
        status:      queued | active | completed | failed | cancelled
                     | timeout | paused
        timestamps:  queued_at, started_at, completed_at + generated
                     duration_ms
        usage:       prompt_tokens, completion_tokens, cache_read,
                     cache_write + generated total_tokens
        cost:        cost_breakdown JSONB (per-provider) + generated
                     total_cost_usd (extracted from JSONB)
        spawn:       parent_run_id (self-FK), turns_used, max_turns,
                     sub_agents_spawned

    `agent_run_events` is append-only telemetry: lifecycle, llm,
    tool, sub_agent, compaction, streaming. Sequence per run.

This migration:
    1. Renames `agents` → `session_agents` (the existing index, FK,
       and `agents.id` PK keep their values - all historical
       action_executions.agent_pk references stay valid).
    2. Creates `agent_runs` and `agent_run_events`.
    3. Adds `action_executions.agent_run_id` (FK on agent_runs.id,
       SET NULL) - kept alongside the legacy `agent_pk` column.
       Application code now writes both during the transition; a
       follow-up sprint will drop `agent_pk` once code stops using it.
    4. Creates the dashboard support views v_agents_running and
       v_agents_top_cost_7d.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # ── 1. Rename agents → session_agents ─────────────────────────
    # idempotent: only rename if `agents` still exists.
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'agents'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'session_agents'
        ) THEN
            ALTER TABLE agents RENAME TO session_agents;
            -- Rename indexes to follow the new table name.
            ALTER INDEX IF EXISTS ix_agents_session_agent
                RENAME TO ix_session_agents_session_agent;
        END IF;
    END
    $$;
    """)

    # ── 2. agent_runs ─────────────────────────────────────────────
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "session_agent_id",
            sa.String(64),
            sa.ForeignKey("session_agents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "session_pk",
            sa.String(64),
            sa.ForeignKey("user_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            comment=(
                "Denormalised session FK for cheap dashboard joins. "
                "Always equals session_agents.session_pk."
            ),
        ),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            comment="Denormalised owner FK for per-user quota / RLS.",
        ),
        sa.Column(
            "parent_run_id",
            sa.String(64),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
            comment="Set when spawned by another agent (sub-agent tree).",
        ),
        # ── lifecycle ────────────────────────────────────────
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="queued",
            comment=(
                "queued | active | completed | failed | cancelled "
                "| timeout | paused"
            ),
        ),
        sa.Column("status_reason", sa.Text, nullable=True),
        # ── inputs ────────────────────────────────────────────
        sa.Column("specialist", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column(
            "fallback_used",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("task_summary", sa.Text, nullable=True),
        # ── budget ────────────────────────────────────────────
        sa.Column(
            "max_turns",
            sa.Integer,
            nullable=True,
            comment="Hard turn cap; null = inherit app default.",
        ),
        sa.Column(
            "turns_used",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "sub_agents_spawned",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        # ── usage (counters) ─────────────────────────────────
        sa.Column(
            "prompt_tokens",
            sa.BigInteger,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "completion_tokens",
            sa.BigInteger,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cache_read_tokens",
            sa.BigInteger,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cache_write_tokens",
            sa.BigInteger,
            nullable=False,
            server_default="0",
        ),
        # ── cost ──────────────────────────────────────────────
        sa.Column(
            "cost_breakdown",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=(
                "Per-provider cost map: "
                "{provider: {input_usd, output_usd, cache_usd, total_usd}}."
            ),
        ),
        # ── timing ────────────────────────────────────────────
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_event_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Heartbeat; updated on every agent_run_events row.",
        ),
        # ── audit ────────────────────────────────────────────
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

    # ── generated columns (Postgres 12+) ─────────────────────
    # total_tokens = prompt + completion + cache_read + cache_write
    op.execute("""
    ALTER TABLE agent_runs
    ADD COLUMN total_tokens BIGINT
    GENERATED ALWAYS AS (
        prompt_tokens + completion_tokens
        + cache_read_tokens + cache_write_tokens
    ) STORED;
    """)
    # duration_ms = completed_at - started_at, in ms; null while active.
    op.execute("""
    ALTER TABLE agent_runs
    ADD COLUMN duration_ms BIGINT
    GENERATED ALWAYS AS (
        CASE
            WHEN started_at IS NOT NULL AND completed_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000
            ELSE NULL
        END
    ) STORED;
    """)
    # `total_cost_usd` is trigger-populated (a GENERATED column
    # can't reference `jsonb_path_query` cleanly).
    op.execute("""
    ALTER TABLE agent_runs
    ADD COLUMN total_cost_usd NUMERIC(14, 6) NOT NULL DEFAULT 0;
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION agent_runs_recompute_cost()
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
    op.execute("""
    CREATE TRIGGER trg_agent_runs_recompute_cost
    BEFORE INSERT OR UPDATE OF cost_breakdown ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION agent_runs_recompute_cost();
    """)

    # ── indexes for hot queries ──────────────────────────────
    op.create_index(
        "ix_agent_runs_status_started",
        "agent_runs",
        ["status", "started_at"],
        postgresql_where=sa.text(
            "status IN ('queued', 'active')"
        ),
    )
    op.create_index(
        "ix_agent_runs_user_completed",
        "agent_runs",
        ["user_id", "completed_at"],
        postgresql_where=sa.text("completed_at IS NOT NULL"),
    )
    op.create_index(
        "ix_agent_runs_session_started",
        "agent_runs",
        ["session_pk", "started_at"],
    )

    # Wire the shared updated_at trigger from Sprint A.
    op.execute("""
    CREATE TRIGGER trg_agent_runs_updated_at
    BEFORE UPDATE ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION digitorn_set_updated_at();
    """)

    # ── 3. agent_run_events ───────────────────────────────────────
    op.create_table(
        "agent_run_events",
        sa.Column(
            "id",
            sa.BigInteger,
            sa.Identity(start=1, cycle=False),
            primary_key=True,
        ),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sequence",
            sa.Integer,
            nullable=False,
            comment="Per-run monotonic counter, starts at 1.",
        ),
        sa.Column(
            "event_type",
            sa.String(32),
            nullable=False,
            comment=(
                "lifecycle | turn | llm | tool | sub_agent | compaction "
                "| streaming"
            ),
        ),
        sa.Column(
            "data",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "elapsed_ms",
            sa.BigInteger,
            nullable=True,
            comment="Milliseconds since the run's started_at.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "run_id", "sequence",
            name="uq_agent_run_events_run_sequence",
        ),
    )
    op.create_index(
        "ix_agent_run_events_run_created",
        "agent_run_events",
        ["run_id", "created_at"],
    )
    op.create_index(
        "ix_agent_run_events_type_created",
        "agent_run_events",
        ["event_type", "created_at"],
    )
    op.create_index(
        "ix_agent_run_events_data_gin",
        "agent_run_events",
        ["data"],
        postgresql_using="gin",
        postgresql_ops={"data": "jsonb_path_ops"},
    )

    # ── 4. action_executions.agent_run_id ─────────────────────────
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'action_executions'
              AND column_name = 'agent_run_id'
        ) THEN
            ALTER TABLE action_executions
              ADD COLUMN agent_run_id VARCHAR(64) NULL
              REFERENCES agent_runs(id) ON DELETE SET NULL;
        END IF;
    END
    $$;
    """)
    op.execute("COMMIT")
    try:
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_action_executions_agent_run_id "
            "ON action_executions (agent_run_id)"
        )
    finally:
        op.execute("BEGIN")

    # ── 5. dashboard support views ─────────────────────────────────
    # v_agents_running: every active or queued run + elapsed time +
    # event count. The dashboard's "Live agents" panel hits this.
    op.execute("""
    CREATE OR REPLACE VIEW v_agents_running AS
    SELECT
        r.id                AS run_id,
        r.session_agent_id,
        sa.agent_id         AS specialist_key,
        sa.name             AS specialist_name,
        r.session_pk,
        r.user_id,
        r.specialist,
        r.provider,
        r.model,
        r.status,
        r.queued_at,
        r.started_at,
        r.last_event_at,
        EXTRACT(EPOCH FROM (
            COALESCE(r.last_event_at, NOW()) - COALESCE(r.started_at, r.queued_at)
        )) * 1000 AS elapsed_ms,
        r.turns_used,
        r.max_turns,
        r.total_tokens,
        r.total_cost_usd,
        (
            SELECT COUNT(*) FROM agent_run_events e WHERE e.run_id = r.id
        ) AS event_count
    FROM agent_runs r
    JOIN session_agents sa ON sa.id = r.session_agent_id
    WHERE r.status IN ('queued', 'active');
    """)

    # v_agents_top_cost_7d: top-50 users by spend over rolling 7 days.
    op.execute("""
    CREATE OR REPLACE VIEW v_agents_top_cost_7d AS
    SELECT
        user_id,
        COUNT(*)                    AS run_count,
        SUM(total_tokens)::BIGINT   AS tokens_7d,
        SUM(total_cost_usd)         AS cost_7d_usd,
        SUM(turns_used)             AS turns_7d,
        AVG(duration_ms)::BIGINT    AS avg_duration_ms
    FROM agent_runs
    WHERE completed_at >= NOW() - INTERVAL '7 days'
      AND status = 'completed'
    GROUP BY user_id
    ORDER BY cost_7d_usd DESC
    LIMIT 50;
    """)


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute("DROP VIEW IF EXISTS v_agents_top_cost_7d")
    op.execute("DROP VIEW IF EXISTS v_agents_running")

    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_action_executions_agent_run_id"
    )
    op.execute(
        "ALTER TABLE action_executions DROP COLUMN IF EXISTS agent_run_id"
    )

    op.drop_table("agent_run_events")

    op.execute("DROP TRIGGER IF EXISTS trg_agent_runs_updated_at ON agent_runs")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_agent_runs_recompute_cost ON agent_runs"
    )
    op.execute("DROP FUNCTION IF EXISTS agent_runs_recompute_cost")
    op.drop_table("agent_runs")

    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'session_agents'
        ) THEN
            ALTER TABLE session_agents RENAME TO agents;
            ALTER INDEX IF EXISTS ix_session_agents_session_agent
                RENAME TO ix_agents_session_agent;
        END IF;
    END
    $$;
    """)
