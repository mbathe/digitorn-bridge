"""ORM models owned by the auth service.

These mirror the schema currently in ``digitorn.core.models`` for the
auth-related tables (``users``, ``user_oauth_tokens``, ``roles``,
``user_roles``, ``refresh_tokens``) so this package can run against the
SAME Postgres as the daemon during the transition. Only the subset auth
needs is duplicated here — application-side tables (``applications``,
``user_sessions``, ``history_log``, etc.) stay owned by the daemon.

The new ``PairedDevice`` table is unique to this service and lives only
here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from digitorn_auth.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex


# ── Users ──────────────────────────────────────────────────────────


class User(Base):
    """A unified user record, source-agnostic.

    Schema-identical to ``digitorn.core.models.User`` so a single
    Postgres can host both packages during the migration.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    app_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    oauth_tokens: Mapped[list["UserOAuthToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_users_app_provider_external", "app_id", "provider", "external_id", unique=True),
        Index("ix_users_app", "app_id"),
    )


class UserOAuthToken(Base):
    """OAuth2 access/refresh tokens for a user, encrypted at rest."""

    __tablename__ = "user_oauth_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    access_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_type: Mapped[str] = mapped_column(String(32), nullable=False, default="bearer")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    user: Mapped["User"] = relationship(back_populates="oauth_tokens")

    __table_args__ = (
        Index("ix_oauth_tokens_user_provider", "user_id", "provider", unique=True),
    )


# ── Roles & permissions ────────────────────────────────────────────


class Role(Base):
    """A role groups a set of permissions. Built-ins: admin, developer, viewer."""

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UserRole(Base):
    """Association between users and roles. Multiple roles per user merge."""

    __tablename__ = "user_roles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    role_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False,
    )
    app_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    granted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_user_roles_user_role_app", "user_id", "role_id", "app_id", unique=True),
        Index("ix_user_roles_user", "user_id"),
    )


# ── Refresh tokens ─────────────────────────────────────────────────


class RefreshToken(Base):
    """Stored refresh tokens — revocable, trackable. Hashed in DB."""

    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    device_info: Mapped[str | None] = mapped_column(String(512), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_refresh_tokens_user", "user_id"),
    )


# ── Device pairing (NEW — owned exclusively by this service) ──────


class PairedDevice(Base):
    """Long-lived pairing between a user and a daemon instance.

    Created once when the user runs ``digitorn install-local`` (or pairs
    a hosted daemon from the dashboard). Carries the metadata the central
    needs to:
      - Mint device tokens that authenticate the daemon offline.
      - Surface "your devices" in the user dashboard with last-seen info.
      - Revoke a device remotely (next periodic ping makes the daemon
        wipe its local secrets and require re-pairing).
    """

    __tablename__ = "paired_devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(
        String(128), nullable=False,
        # User-chosen ("MacBook-Paul", "Office Pi", "VPS Hetzner-FRA").
        # Default to hostname when the CLI provisions it.
    )

    # Optional: daemon-side public key for proof-of-possession on
    # revalidate requests. Populated by future versions of the CLI;
    # nullable for backwards compat with the v1 pairing flow that
    # only carries the central-signed device token.
    daemon_public_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Last issued device_token's jti — lets the central detect "the
    # token I'm seeing isn't the one I last issued" (token replay /
    # copied to another machine). Optional defense in depth.
    last_token_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Telemetry surfaced in the dashboard.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen_user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Lifecycle.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


# ── Account features (NEW — drives cloud vs self-host gating) ──────


class AccountFeatures(Base):
    """Per-user feature flags + quotas baked into JWT claims.

    Single row per user (1:1). Lookup is keyed by user_id, NOT a
    surrogate id, so claims propagation is one DB hit at login time
    and gets cached in the access token for the next 15 min.

    The daemon (and any other consumer) reads these via the
    ``features`` claim on the JWT and decides:
      * ``cloud_enabled``: can this user use the hosted Digitorn cloud?
      * ``self_host_enabled``: is self-hosting allowed for this plan?
      * ``plan_tier``: free / pro / enterprise (drives UI affordances).
      * ``cloud_token_quota_monthly``: max tokens billed against the
        cloud per calendar month (0 = unlimited).
      * ``max_paired_devices``: hard cap on simultaneously-paired
        daemons (default: 5 for free, 100 for pro, ∞ for enterprise).
    """

    __tablename__ = "account_features"

    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    plan_tier: Mapped[str] = mapped_column(
        String(32), nullable=False, default="free",
    )
    cloud_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    self_host_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cloud_token_quota_monthly: Mapped[int] = mapped_column(Integer, default=0)
    max_paired_devices: Mapped[int] = mapped_column(Integer, default=5)
    flags: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False,
        # Bag for future flags we don't want to migrate the schema for
        # every time. e.g. {"beta_features": ["agent-mesh"], "rate_limit_override": 200}
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


# ── Revocation list (NEW — instant token kill switch) ──────────────


class RevokedToken(Base):
    """JWT IDs (jti) the auth service has explicitly revoked.

    Stateless JWTs normally expire on their own at ``exp``. For
    high-security flows we want INSTANT revocation: user clicks
    "log out everywhere", admin disables a compromised account,
    employee leaves the company. This table stores per-jti revoke
    rows; daemons fetch the active list (or check on-demand) and
    refuse any token whose ``jti`` is here.

    Rows are auto-pruned by ``AuthService._prune_revocations`` once
    ``expires_at`` has passed — there's no point storing a revoked
    jti past its natural expiry, the signature check would already
    fail.
    """

    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Original token's `exp` claim — once now() > expires_at we can
    # drop the row (the token is naturally invalid past that point).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False, default="user_logout")
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
