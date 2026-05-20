"""Sprint F - notifications fusion + feature flags + audit catalog (Postgres only).

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-05

Three additive moves:

1. Fuse `inbox_devices` and `inbox_notification_prefs` into a
   single `user_devices` table. The split was historical: prefs
   were per-user (one row), devices were 0..N. The runtime now needs
   per-device prefs (a user disables push on their phone but keeps it
   on their tablet), and joining the two tables on every push send is
   wasteful. Result: one row per (user_id, fcm_token) with
   `prefs` JSONB inline.

2. NEW `feature_flags` table for runtime feature gating
   (% rollout, allowlist, conditions). Replaces ad-hoc env vars.

3. NEW `audit_actions_catalog` listing every action_key the
   `credential_audit` / `history_log` tables reference, with
   severity + retention. Lets the dashboard show human labels and
   the cleanup job apply per-action retention.

4. Soft delete columns on `applications` and `agent_runs`.

Idempotent (CREATE TABLE IF NOT EXISTS, ALTER COLUMN IF NOT EXISTS).
The migration COPIES from the legacy tables into `user_devices` and
LEAVES the legacy tables in place. A follow-up sprint can drop them
once all writes are routed to user_devices.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # ── 1. user_devices (fusion of inbox_devices + inbox_notification_prefs) ──
    op.create_table(
        "user_devices",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "platform",
            sa.String(16),
            nullable=False,
            comment="ios | android | web | macos | windows | linux",
        ),
        sa.Column("device_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("app_version", sa.String(32), nullable=False, server_default=""),
        sa.Column(
            "fcm_token",
            sa.Text,
            nullable=False,
            comment="FCM / APNS / web-push token. Unique per (user_id).",
        ),
        sa.Column(
            "prefs",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=(
                "Per-device preferences: notification kinds enabled, "
                "quiet hours, sound, vibrate, etc."
            ),
        ),
        sa.Column(
            "subscribed_topics",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="FCM topic subscriptions for broadcast triggers.",
        ),
        sa.Column(
            "active",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
            comment=(
                "False = device unregistered (FCM 410 / user logout). "
                "Soft delete; we keep the row for analytics."
            ),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "registered_at",
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
        sa.UniqueConstraint(
            "user_id", "fcm_token",
            name="uq_user_devices_user_token",
        ),
    )
    op.create_index(
        "ix_user_devices_active_seen",
        "user_devices",
        ["active", "last_seen_at"],
    )
    op.execute(
        "CREATE TRIGGER trg_user_devices_updated_at "
        "BEFORE UPDATE ON user_devices "
        "FOR EACH ROW EXECUTE FUNCTION digitorn_set_updated_at()"
    )

    # Backfill: merge `inbox_notification_prefs` (user-level) into
    # the per-device row; first device wins, the rest can retune.
    op.execute("""
    INSERT INTO user_devices (
        id, user_id, platform, device_name, app_version, fcm_token,
        prefs, last_seen_at, registered_at, updated_at, active
    )
    SELECT
        d.id,
        d.user_id,
        d.platform,
        COALESCE(d.device_name, ''),
        COALESCE(d.app_version, ''),
        d.fcm_token,
        COALESCE(p.prefs, '{}'::jsonb),
        d.last_seen_at,
        d.created_at,
        NOW(),
        TRUE
    FROM inbox_devices d
    LEFT JOIN inbox_notification_prefs p ON p.user_id = d.user_id
    WHERE NOT EXISTS (
        SELECT 1 FROM user_devices ud
        WHERE ud.user_id = d.user_id AND ud.fcm_token = d.fcm_token
    );
    """)

    # ── 2. feature_flags ──────────────────────────────────────────
    op.create_table(
        "feature_flags",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "scope",
            sa.String(16),
            nullable=False,
            server_default="global",
            comment="global | per_user | per_app",
        ),
        sa.Column(
            "enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "rollout_percent",
            sa.Integer,
            nullable=False,
            server_default="0",
            comment="0..100; deterministic hash of (key, user_id) decides eligibility.",
        ),
        sa.Column(
            "conditions",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=(
                "Optional matcher: "
                "{user_ids: [...], plan_ids: [...], app_ids: [...], "
                "min_app_version: '1.2.0', ...}"
            ),
        ),
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
        sa.CheckConstraint(
            "rollout_percent >= 0 AND rollout_percent <= 100",
            name="ck_feature_flags_rollout_percent",
        ),
    )
    op.execute(
        "CREATE TRIGGER trg_feature_flags_updated_at "
        "BEFORE UPDATE ON feature_flags "
        "FOR EACH ROW EXECUTE FUNCTION digitorn_set_updated_at()"
    )

    # ── 3. audit_actions_catalog ──────────────────────────────────
    op.create_table(
        "audit_actions_catalog",
        sa.Column("action_key", sa.String(64), primary_key=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            server_default="info",
            comment="debug | info | notice | warning | critical",
        ),
        sa.Column(
            "retention_days",
            sa.Integer,
            nullable=False,
            server_default="365",
            comment="Days to keep matching history_log / credential_audit rows.",
        ),
        sa.Column(
            "category",
            sa.String(32),
            nullable=False,
            server_default="other",
            comment="auth | credential | quota | agent | session | admin | other",
        ),
        sa.Column(
            "ui_label",
            sa.String(128),
            nullable=True,
            comment="Human-friendly label for the dashboard.",
        ),
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
    op.execute(
        "CREATE TRIGGER trg_audit_actions_catalog_updated_at "
        "BEFORE UPDATE ON audit_actions_catalog "
        "FOR EACH ROW EXECUTE FUNCTION digitorn_set_updated_at()"
    )

    # Seed canonical action_keys we already emit. Idempotent via ON CONFLICT.
    op.execute("""
    INSERT INTO audit_actions_catalog
        (action_key, description, severity, category, retention_days, ui_label)
    VALUES
        ('credential.create', 'Credential created', 'info', 'credential', 730,
         'Created credential'),
        ('credential.update', 'Credential updated', 'info', 'credential', 730,
         'Updated credential'),
        ('credential.delete', 'Credential deleted', 'warning', 'credential', 1825,
         'Deleted credential'),
        ('credential.access', 'Credential read at runtime', 'debug', 'credential', 90,
         'Used credential'),
        ('quota.block', 'Quota block fired', 'notice', 'quota', 365,
         'Quota exceeded'),
        ('quota.unblock', 'Quota block cleared', 'info', 'quota', 365,
         'Quota cleared'),
        ('agent.spawn', 'Agent spawned', 'info', 'agent', 180,
         'Agent started'),
        ('agent.complete', 'Agent run completed', 'info', 'agent', 180,
         'Agent finished'),
        ('agent.fail', 'Agent run failed', 'warning', 'agent', 365,
         'Agent failed'),
        ('session.create', 'Session created', 'debug', 'session', 90, 'New session'),
        ('session.delete', 'Session deleted', 'info', 'session', 365,
         'Deleted session'),
        ('admin.role_grant', 'Role granted to user', 'notice', 'admin', 1825,
         'Granted role'),
        ('admin.role_revoke', 'Role revoked from user', 'notice', 'admin', 1825,
         'Revoked role'),
        ('auth.login', 'User login', 'debug', 'auth', 90, 'Login'),
        ('auth.logout', 'User logout', 'debug', 'auth', 90, 'Logout'),
        ('auth.refresh', 'Token refreshed', 'debug', 'auth', 30, 'Refresh'),
        ('plan.assign', 'Plan assigned', 'notice', 'admin', 1825, 'Plan changed'),
        ('plan.cancel', 'Plan cancelled', 'notice', 'admin', 1825, 'Plan cancelled')
    ON CONFLICT (action_key) DO NOTHING;
    """)

    # ── 4. soft delete columns ────────────────────────────────────
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'applications' AND column_name = 'deleted_at'
        ) THEN
            ALTER TABLE applications ADD COLUMN deleted_at TIMESTAMPTZ NULL;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'agent_runs' AND column_name = 'deleted_at'
        ) THEN
            ALTER TABLE agent_runs ADD COLUMN deleted_at TIMESTAMPTZ NULL;
        END IF;
    END
    $$;
    """)
    op.execute("COMMIT")
    try:
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_applications_active "
            "ON applications (app_id) WHERE deleted_at IS NULL"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_agent_runs_active "
            "ON agent_runs (status, started_at) "
            "WHERE deleted_at IS NULL"
        )
    finally:
        op.execute("BEGIN")


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_agent_runs_active"
    )
    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_applications_active"
    )
    op.execute("ALTER TABLE agent_runs DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE applications DROP COLUMN IF EXISTS deleted_at")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_audit_actions_catalog_updated_at "
        "ON audit_actions_catalog"
    )
    op.drop_table("audit_actions_catalog")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_feature_flags_updated_at "
        "ON feature_flags"
    )
    op.drop_table("feature_flags")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_user_devices_updated_at "
        "ON user_devices"
    )
    op.drop_index("ix_user_devices_active_seen", table_name="user_devices")
    op.drop_table("user_devices")
