"""SQLAlchemy ORM models for the gateway quota schema.

Three tables, all in the same DB as `auth.users`:

* ``plans``                - tier definitions (free / pro / enterprise / ...).
                             The full QuotaDefinition shape lives in a JSONB
                             column so we can ship a richer policy without
                             schema changes per metric / window addition.

* ``user_plans``           - association ``user_id -> plan_id`` plus an
                             optional ``override_quota_def`` JSONB for the
                             rare case where one specific user gets a
                             custom envelope without a whole new plan.

* ``quota_counters``       - durable per-user, per-metric, per-window
                             counter rows. The hot-path counter lives in
                             memory; this table is the periodic flush
                             target so we survive a gateway restart.

The `auth.users` table itself is NOT modified - we link via FK on
``user_plans.user_id``. That keeps the auth migration story owned by
the auth package alone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    """A tier (free, pro, enterprise, ...). All quota envelopes live
    in the JSONB ``quota_def`` column - same shape as the existing
    ``QuotaDefinition`` (6 metrics x 5 windows + per-model overrides).

    The default plan (`is_default=True`) is the one assigned to users
    with no explicit ``user_plans`` row. Exactly one plan should carry
    that flag; the boot-time seeder enforces it.
    """

    __tablename__ = "gateway_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False)
    quota_def: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_gateway_plans_is_default", "is_default"),
    )


class UserPlan(Base):
    """One row per user that has been bound to a plan. Users with no
    row default to the `is_default=True` plan at runtime. The
    ``override_quota_def`` column lets an admin pin custom limits
    on a single user without forking the plan - reserved for the
    enterprise case ("this customer paid for unlimited tokens, but
    we don't want a whole new plan tier just for them").
    """

    __tablename__ = "gateway_user_plans"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("gateway_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    override_quota_def: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, default=None,
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_gateway_user_plans_plan_id", "plan_id"),
    )


class QuotaCounter(Base):
    """Persistent counter row. The in-memory engine owns the hot
    path; this table is its checkpoint.

    Composite key: ``(user_id, metric, window_key)``. ``window_key`` is
    the start of the bucket (``YYYY-MM-DD-HH`` for hourly, etc.)
    plus the strategy tag - any string the engine decides to use, as
    long as the (user, metric, key) tuple is unique.

    ``reset_at`` is the timestamp at which the counter restarts at zero.
    Old rows are garbage-collected by the periodic flush task.
    """

    __tablename__ = "gateway_quota_counters"

    # Using BigInteger + Identity instead of `autoincrement=True` because
    # SQLite ignores autoincrement on BigInteger (only INTEGER PRIMARY KEY
    # auto-increments natively). Identity translates to SERIAL/IDENTITY on
    # Postgres and to INTEGER PRIMARY KEY ROWID on SQLite.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(start=1, always=False),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    window_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reset_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "metric", "window_key",
            name="uq_quota_counter_user_metric_window",
        ),
        Index("ix_gateway_quota_counters_user_id", "user_id"),
        Index("ix_gateway_quota_counters_reset_at", "reset_at"),
    )


class QuotaBlock(Base):
    """Sticky block records. When a user crosses a hard limit, the
    engine writes a row here so the block survives a gateway restart.
    Cleared when ``blocked_until`` is in the past, or by an explicit
    admin reset.
    """

    __tablename__ = "gateway_quota_blocks"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    blocked_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(256), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    window: Mapped[str] = mapped_column(String(32), nullable=False)
    limit_value: Mapped[float] = mapped_column(Float, nullable=False)
    actual_value: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_gateway_quota_blocks_blocked_until", "blocked_until"),
    )
