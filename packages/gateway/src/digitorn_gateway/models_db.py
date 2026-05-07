"""SQLAlchemy ORM models for the gateway quota schema.

The gateway connects to the SAME production Postgres as digitorn-auth.
There is no separate gateway database, no SQLite dev fallback, and no
synchronization of user identifiers: the JWT's ``sub`` claim IS the
``users.id`` UUID, and the gateway's foreign keys reference it
directly.

Tables added by the gateway, all in the shared Postgres alongside
``users``, ``user_oauth_tokens``, ``roles``, etc.:

* ``gateway_plans``           - tier definitions (free / pro / enterprise).
* ``gateway_user_plans``      - association user_id -> plan_id (FK to users).
* ``gateway_quota_counters``  - per-user counter rows (FK to users).
* ``gateway_quota_blocks``    - sticky block records (FK to users).

ON DELETE CASCADE: when an admin deletes a user from auth, every
gateway row tied to them disappears. Analytics that need to survive
a deletion must be exported off-band before the user is removed.

The ``users`` table itself is owned by ``digitorn-auth``. The gateway
never inserts into it - if a JWT carries a ``sub`` that doesn't match
an existing user row, the FK insert fails and the request is rejected.
This is the contract: identity is auth's responsibility.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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

    # FK to the auth `users.id`. Same UUID emitted by digitorn-auth in
    # the JWT `sub` claim - so a user's row here is always reachable
    # from the row in auth without any translation table.
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
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

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    # Same auth `users.id`. CASCADE on delete so usage rows go away
    # with the user; analytics that need to survive a deletion should
    # be exported off-band before the user is removed.
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
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

    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
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


# ── Dashboard-writable config (Sprint F, 2026-05-06) ─────────────────


class GatewayProvider(Base):
    """LLM provider declared by an operator. The slug is canonical
    (matches LiteLLM's provider id when ``compat == 'openai'`` /
    ``'anthropic'`` etc.). ``base_url`` is only used for OpenAI-compat
    providers that aren't on the LiteLLM hardcoded list."""

    __tablename__ = "gateway_providers"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    compat: Mapped[str] = mapped_column(
        String(32), nullable=False, default="openai",
    )
    env_var: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="api_key",
    )
    auth_schema: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list,
    )
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict,
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
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


class GatewayCredential(Base):
    """An encrypted API key for one provider. Multiple credentials per
    provider allowed (``Production`` / ``Dev`` / ``Team-A`` etc.).

    ``encrypted_value`` carries the envelope blob built by
    ``cipher.encrypt(plaintext)``; the gateway never logs the
    plaintext and never returns it via the HTTP API."""

    __tablename__ = "gateway_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    provider_slug: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("gateway_providers.slug", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    cipher_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active",
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # When True the dispatch path keeps an ``httpx.AsyncClient`` warm
    # for this credential and passes it to LiteLLM via ``client=``.
    # Saves the TCP + TLS handshake (100-300ms) on every call. The
    # operator can opt out from the dashboard for memory-constrained
    # deploys; for ``aws_bedrock`` / ``vertex_ai`` auth_types the
    # toggle is informational (their SDKs pool natively).
    live_pool: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "provider_slug", "label", name="uq_gateway_credentials_label",
        ),
        Index(
            "ix_gateway_credentials_provider_status",
            "provider_slug", "status",
        ),
    )


class GatewayModel(Base):
    """One catalogue entry. Replaces a row in the legacy
    ``models.yaml`` file once the bootstrap seeder has copied it over.
    """

    __tablename__ = "gateway_models"

    alias: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider_slug: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("gateway_providers.slug", ondelete="RESTRICT"),
        nullable=False,
    )
    real_model_id: Mapped[str] = mapped_column(Text, nullable=False)
    cost_per_1k_input_tokens: Mapped[float] = mapped_column(
        Numeric(12, 6), nullable=False, default=0,
    )
    cost_per_1k_output_tokens: Mapped[float] = mapped_column(
        Numeric(12, 6), nullable=False, default=0,
    )
    max_context_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    is_custom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict,
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
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
        Index("ix_gateway_models_provider", "provider_slug"),
    )


class GatewayRoute(Base):
    """Ordered credential bindings for a model alias.

    A model can have N routes, each with a priority (0 = primary,
    higher = fallback). The dispatcher walks them in ascending order
    and picks the first healthy one. Deleting all routes makes the
    alias unusable until a new credential is wired.

    Cross-provider routing (added 2026-05-07): each route carries its
    own provider/real_model_id/compat/base_url/dispatch_headers, so the
    primary for ``claude-opus-4`` can be GitHub Copilot while the
    fallback hits Anthropic direct. The constraint becomes
    ``credential.provider_slug == route.provider_slug`` (enforced by
    the API, not the DB, since credentials live in another table).
    The ``gateway_models`` row stays as a metadata anchor (cost +
    context window + display name) but no longer dictates the dispatch
    target."""

    __tablename__ = "gateway_routes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    model_alias: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("gateway_models.alias", ondelete="CASCADE"),
        nullable=False,
    )
    credential_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gateway_credentials.id", ondelete="RESTRICT"),
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    # Per-route dispatch identity (cross-provider routing). NOT NULL
    # after the 0010 backfill migration; new rows must always provide
    # them. ``base_url`` and ``dispatch_headers`` stay nullable / empty
    # since they're optional overrides on top of the provider defaults.
    provider_slug: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("gateway_providers.slug", ondelete="RESTRICT"),
        nullable=False,
    )
    real_model_id: Mapped[str] = mapped_column(Text, nullable=False)
    compat: Mapped[str] = mapped_column(
        String(32), nullable=False, default="openai",
    )
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatch_headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "model_alias", "priority", name="uq_gateway_routes_alias_priority",
        ),
        Index(
            "ix_gateway_routes_alias_priority_asc",
            "model_alias", "priority",
        ),
        Index(
            "ix_gateway_routes_provider_slug",
            "provider_slug",
        ),
    )
