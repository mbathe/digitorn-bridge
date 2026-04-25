"""Digitorn — ORM models for persistence.

Tables follow the isolation hierarchy:
    Application → UserSession → Agent → ActionExecution

All executions are persisted with their full scope (app_id, session_id,
agent_id) and linked to the event system via correlation_id.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from digitorn.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex


class EncryptedJSON(TypeDecorator):
    """SQLAlchemy type that stores JSON data encrypted at rest.

    On write: Python dict/list -> JSON string -> Fernet encrypt -> LargeBinary
    On read:  LargeBinary -> Fernet decrypt -> JSON parse -> Python dict/list

    Uses the server-level key from ~/.digitorn/server.key (auto-generated).
    Backward-compatible: reads plain JSON for data written before encryption.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> bytes | None:
        if value is None:
            return None
        import json as _json
        from digitorn.core.crypto import encrypt_value
        return encrypt_value(_json.dumps(value, ensure_ascii=False))

    def process_result_value(self, value: bytes | None, dialect: Any) -> Any:
        if value is None:
            return None
        import json as _json
        from digitorn.core.crypto import decrypt_value
        try:
            return _json.loads(decrypt_value(value))
        except Exception:
            # Fallback: try reading as plain JSON (unencrypted legacy data)
            try:
                raw = value.decode("utf-8") if isinstance(value, bytes) else value
                return _json.loads(raw)
            except Exception:
                logger.debug("Failed to decode encrypted column value", exc_info=True)
                return {}


class Application(Base):
    """A registered application that connects to Digitorn.

    A deployed app is backed by an immutable **AppBundle** that contains
    the YAML source and every file it references (skills, agent prompts,
    any other asset). The bundle is the single source of truth after the
    initial deploy — the daemon never reaches back to the original source
    filesystem to reload an app.

    The ``current_bundle_id`` FK points to the active bundle. Previous
    bundles can be kept around for rollback (the AppBundle rows are not
    automatically deleted when a new bundle replaces them).
    """

    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # ── Multi-tenant scoping ─────────────────────────────────
    # An `app_id` by itself is NOT unique anymore. Two users can each have
    # their own "my-app", and the same name can also exist as a system
    # install. Uniqueness is enforced on the composite
    # (app_id, scope, owner_user_id).
    #
    # - scope="system", owner_user_id="" → install visible to every user
    # - scope="user",   owner_user_id="alice" → private to Alice
    #
    # We store "" (empty string) instead of NULL for owner_user_id in the
    # system case so the unique index works reliably on SQLite, which
    # otherwise treats NULL as distinct inside unique constraints.
    scope: Mapped[str] = mapped_column(
        String(16), default="system", server_default="system", nullable=False,
        comment="Install scope: 'system' (global) or 'user' (per-user install).",
    )
    owner_user_id: Mapped[str] = mapped_column(
        String(64), default="", server_default="", nullable=False,
        comment="User who owns a user-scoped install; empty string for system scope.",
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="1.0")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    # Legacy columns — kept for backward-compat with pre-bundle deploys.
    # New deploys write to the AppBundle table instead.
    yaml_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    yaml_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    yaml_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Pointer to the currently active bundle (the one used on reload).
    # Nullable only for legacy rows — new deploys always set it.
    current_bundle_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("app_bundles.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # ── App Packages support (v1: source attribution) ──
    # When an app was installed via the AppPackages system, these
    # fields tell us which package it came from. Existing apps
    # deployed via the legacy /api/apps/deploy route get
    # source_type="local" and package_id=NULL automatically at boot
    # (see digitorn.core.packages.migration.classify_existing_apps).
    package_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Links to installed_packages.package_id when this app came from a package",
    )
    source_type: Mapped[str] = mapped_column(
        String(16), default="local",
        comment="local | builtin | hub | git — how this app was installed",
    )
    package_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="SHA-256 of the package content at install time, for drift detection",
    )

    # ── Disable support ────────────────────────────────────────
    # When `disabled=True`, the app is NOT reloaded at daemon startup, is
    # hidden from list_apps/get for non-admins, and session creation is
    # refused. Only an admin can re-enable (via POST /api/apps/{id}/enable).
    # Bundles + history + sessions are preserved — disable is reversible.
    disabled: Mapped[bool] = mapped_column(
        default=False, server_default="0", nullable=False,
        comment="True = app hidden + unusable; only admin can re-enable.",
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="When the app was disabled (UTC).",
    )
    disabled_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Optional free-text reason supplied by the caller.",
    )

    sessions: Mapped[list["UserSession"]] = relationship(back_populates="application", cascade="all, delete-orphan")
    security_profile: Mapped["AppProfile | None"] = relationship(back_populates="application", cascade="all, delete-orphan", uselist=False)
    module_configs: Mapped[list["AppModuleConfig"]] = relationship(back_populates="application", cascade="all, delete-orphan")
    # ``bundles`` was previously cascade="all, delete-orphan" via an
    # FK on AppBundle.app_id. With the scoping refactor the FK is gone
    # (composite keys can't be represented as a single FK in SQLite),
    # so this relationship is now read-only — actual cascade is done
    # explicitly in ``manager.delete_app`` via scoped SQL.
    bundles: Mapped[list["AppBundle"]] = relationship(
        back_populates="application",
        foreign_keys="AppBundle.app_id",
        primaryjoin=(
            "and_("
            "AppBundle.app_id == Application.app_id, "
            "AppBundle.scope == Application.scope, "
            "AppBundle.owner_user_id == Application.owner_user_id"
            ")"
        ),
        viewonly=True,
    )
    current_bundle: Mapped["AppBundle | None"] = relationship(
        foreign_keys=[current_bundle_id],
        post_update=True,
    )

    __table_args__ = (
        # Composite uniqueness: one install per (app_id, scope, owner_user_id).
        # Allows Alice + Bob + system to coexist with the same app_id.
        Index(
            "ix_applications_scope_key",
            "app_id", "scope", "owner_user_id",
            unique=True,
        ),
    )


class AppBundle(Base):
    """An immutable snapshot of a deployed application.

    Every time an app is deployed (or re-deployed with changes), the
    compiler walks the YAML, reads every referenced file (skills, agent
    prompts, etc.) and freezes the whole set into an AppBundle. The
    bundle's content is written to disk under
    ``~/.digitorn/apps/<app_id>/bundle-<short_hash>/`` and is the ONLY
    source the daemon uses to reload the app after a restart. The
    original source directory can be deleted, moved, or modified — the
    deployed app keeps working.

    ``bundle_hash`` is a deterministic SHA-256 over the YAML plus every
    asset (sorted by relative path), so two deploys of the same content
    produce the same bundle_id and are deduplicated.
    """

    __tablename__ = "app_bundles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    # No FK here anymore — ``applications.app_id`` is not unique in the
    # multi-tenant schema (the unique key is now the composite
    # ``(app_id, scope, owner_user_id)``). Deletes cascade at the
    # application layer via ``delete_app`` / ``disable_app``.
    app_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True,
    )
    # Scope partitioning — mirrors Application. Two users can hold a
    # bundle for the same ``app_id`` simultaneously without collision.
    scope: Mapped[str] = mapped_column(
        String(16), default="system", server_default="system", nullable=False,
    )
    owner_user_id: Mapped[str] = mapped_column(
        String(64), default="", server_default="", nullable=False,
    )
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    bundle_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    yaml_filename: Mapped[str] = mapped_column(String(255), nullable=False, default="app.yaml")
    asset_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )

    application: Mapped["Application"] = relationship(
        back_populates="bundles",
        foreign_keys=[app_id],
        primaryjoin=(
            "and_("
            "AppBundle.app_id == Application.app_id, "
            "AppBundle.scope == Application.scope, "
            "AppBundle.owner_user_id == Application.owner_user_id"
            ")"
        ),
        viewonly=True,
    )

    __table_args__ = (
        Index(
            "ix_app_bundles_scope_key",
            "app_id", "scope", "owner_user_id", "bundle_hash",
            unique=True,
        ),
    )


class AppProfile(Base):
    """Security profile for an application.

    Each application has exactly one profile that defines:
    - Default action policy (auto/approve/block)
    - Per-risk-level approval rules
    - Granted permissions (glob patterns supported)
    - Maximum risk level the app can handle
    """

    __tablename__ = "app_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("applications.app_id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    default_policy: Mapped[str] = mapped_column(String(32), default="approve")
    granted_permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    max_risk_level: Mapped[str] = mapped_column(String(32), default="medium")
    risk_approval_rules: Mapped[dict[str, str]] = mapped_column(
        JSON, default=lambda: {"low": "auto", "medium": "approve", "high": "block"}
    )
    approval_timeout: Mapped[float] = mapped_column(Float, default=300.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    application: Mapped["Application"] = relationship(back_populates="security_profile")
    module_grants: Mapped[list["AppModuleGrant"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class AppModuleGrant(Base):
    """Per-module security configuration for an application.

    Controls:
    - visibility: "full" (agent sees the module) or "hidden" (invisible)
    - default_action_policy: default policy for actions in this module
    - action_overrides: per-action policy overrides (JSON dict)
      e.g. {"read_file": "auto", "delete_file": "block"}
    """

    __tablename__ = "app_module_grants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("app_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    module_id: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[str] = mapped_column(String(32), default="full")
    default_action_policy: Mapped[str] = mapped_column(String(32), default="approve")
    action_overrides: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    profile: Mapped["AppProfile"] = relationship(back_populates="module_grants")

    __table_args__ = (
        Index("ix_app_module_grants_profile_module", "profile_id", "module_id", unique=True),
    )


class AppModuleConfig(Base):
    """Per-module configuration and constraints from the app YAML.

    Persists the ``config`` (static settings) and ``constraints`` (runtime
    restrictions) sections of each module block.  The YAML is the source
    of truth — this table is rebuilt on every sync.
    """

    __tablename__ = "app_module_configs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("applications.app_id", ondelete="CASCADE"),
        nullable=False,
    )
    module_id: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    application: Mapped["Application"] = relationship(back_populates="module_configs")

    __table_args__ = (
        Index("ix_app_module_configs_app_module", "app_id", "module_id", unique=True),
    )


class AppSecret(Base):
    """Encrypted secret for an application.

    Secrets are stored encrypted at rest using Fernet symmetric encryption
    (same key as OAuth tokens).  They are resolved via ``{{secret.KEY}}``
    in app YAML and injected at compile time.
    """

    __tablename__ = "app_secrets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("applications.app_id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_app_secrets_app_key", "app_id", "key", unique=True),
    )


class User(Base):
    """A unified user record, source-agnostic.

    Users can originate from any identity provider (local DB, OAuth,
    Active Directory, SAML, custom API).  ``app_id`` is nullable —
    NULL means the user is cross-app (shared across all applications).

    The ``attributes`` JSON column is an extensible bag for custom fields
    that don't warrant a schema migration (e.g. department, locale,
    preferred_language).
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    app_id: Mapped[str | None] = mapped_column(
        String(255),
        ForeignKey("applications.app_id", ondelete="CASCADE"),
        nullable=True,
    )
    email: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    oauth_tokens: Mapped[list["UserOAuthToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_users_app_provider_external", "app_id", "provider", "external_id", unique=True),
        Index("ix_users_app", "app_id"),
    )


class UserOAuthToken(Base):
    """OAuth2 tokens for a user.

    Tokens are encrypted at rest using Fernet symmetric encryption.
    The encryption key is stored in ``~/.digitorn/server.key`` (auto-
    generated on first use) or overridden via ``server.token_encryption_key``.
    """

    __tablename__ = "user_oauth_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    access_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_type: Mapped[str] = mapped_column(String(32), nullable=False, default="bearer")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    user: Mapped["User"] = relationship(back_populates="oauth_tokens")

    __table_args__ = (
        Index("ix_oauth_tokens_user_provider", "user_id", "provider", unique=True),
    )


class UserSession(Base):
    """A user session within an application."""

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("applications.app_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    application: Mapped["Application"] = relationship(back_populates="sessions")
    user: Mapped["User | None"] = relationship(back_populates="sessions")
    agents: Mapped[list["Agent"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    messages: Mapped[list["SessionMessage"]] = relationship(cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_user_sessions_app_session", "app_id", "session_id", unique=True),
    )


class Agent(Base):
    """An agent operating within a user session."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    session_pk: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("user_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    session: Mapped["UserSession"] = relationship(back_populates="agents")
    executions: Mapped[list["ActionExecution"]] = relationship(back_populates="agent", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_agents_session_agent", "session_pk", "agent_id", unique=True),
    )


class ActionExecution(Base):
    """A persisted record of a module action execution."""

    __tablename__ = "action_executions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)

    app_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    module_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(
        Enum("started", "completed", "failed", name="execution_status"),
        default="started",
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    agent_pk: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    agent: Mapped["Agent | None"] = relationship(back_populates="executions")

    __table_args__ = (
        Index("ix_executions_app_module", "app_id", "module_id"),
        Index("ix_executions_app_session", "app_id", "session_id"),
    )


class SessionMessage(Base):
    """A single message in a conversation session.

    Every message exchanged between user, assistant, and tools is persisted.
    On daemon restart, sessions are rebuilt from these rows.
    Ordering is guaranteed by (session_pk, seq) — seq is monotonically
    increasing within a session.
    """

    __tablename__ = "session_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    session_pk: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("user_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # system|user|assistant|tool
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_session_messages_session_seq", "session_pk", "seq"),
        Index("ix_session_messages_session", "session_pk"),
    )


class SessionCheckpoint(Base):
    """Durable checkpoint of a session's execution state.

    Saved after each agent turn. Contains everything needed to resume
    the session exactly where it left off after a daemon restart.
    """

    __tablename__ = "session_checkpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("applications.app_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    turn: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )  # active|completed|failed|paused
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    memory_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # DEPRECATED: workbench has been removed. Column kept for DB migration compatibility.
    workbench_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_checkpoints_app_session", "app_id", "session_id"),
        Index("ix_checkpoints_status", "status"),
    )


class ManagedMCPServer(Base):
    """An MCP server installed and managed at the daemon level.

    MCP servers are first-class daemon resources — they must be installed
    and tested via the daemon before any app can reference them.  This
    ensures security, configuration, and health are validated centrally.

    Lifecycle:  search → install → test → configure → assign to app(s)
    """

    __tablename__ = "managed_mcp_servers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    server_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[str] = mapped_column(String(32), nullable=False, default="catalog")

    transport: Mapped[str] = mapped_column(String(32), nullable=False, default="stdio")
    command: Mapped[str | None] = mapped_column(String(512), nullable=True)
    args: Mapped[list[str]] = mapped_column(JSON, default=list)
    env: Mapped[dict[str, str]] = mapped_column(EncryptedJSON, default=dict)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    headers: Mapped[dict[str, str]] = mapped_column(EncryptedJSON, default=dict)

    runtime: Mapped[str] = mapped_column(String(16), nullable=False, default="npm")
    package: Mapped[str | None] = mapped_column(String(512), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    config: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON, default=dict)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="installed")
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    tools_count: Mapped[int] = mapped_column(Integer, default=0)
    tools_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    last_health_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    health_ok: Mapped[bool] = mapped_column(Boolean, default=False)

    timeout: Mapped[float] = mapped_column(Float, default=30.0)
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_start: Mapped[bool] = mapped_column(Boolean, default=True)

    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_managed_mcp_status", "status"),
    )


class Role(Base):
    """A role defines a set of permissions.

    Built-in roles: admin, developer, viewer.
    Custom roles can be created via API.
    """

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user_roles: Mapped[list["UserRole"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class UserRole(Base):
    """Association between users and roles.

    A user can have multiple roles. Permissions are merged (union).
    Scoped by app_id: NULL means the role applies globally.
    """

    __tablename__ = "user_roles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[str | None] = mapped_column(
        String(255),
        ForeignKey("applications.app_id", ondelete="CASCADE"),
        nullable=True,
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    granted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    role: Mapped["Role"] = relationship(back_populates="user_roles")

    __table_args__ = (
        Index("ix_user_roles_user_app", "user_id", "app_id"),
        Index("ix_user_roles_unique", "user_id", "role_id", "app_id", unique=True),
    )


class RefreshToken(Base):
    """Stored refresh tokens — revocable, trackable.

    Access tokens are stateless (JWT). Refresh tokens are stored in DB
    so they can be revoked (logout, security incident).
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    device_info: Mapped[str | None] = mapped_column(String(512), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_refresh_tokens_user", "user_id"),
    )


class APIKey(Base):
    """API keys for machine-to-machine authentication.

    Keys are hashed (SHA-256). The raw key is shown once at creation.
    Scoped by app_id: NULL means the key works across all apps.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    app_id: Mapped[str | None] = mapped_column(
        String(255),
        ForeignKey("applications.app_id", ondelete="CASCADE"),
        nullable=True,
    )
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_api_keys_user", "user_id"),
        Index("ix_api_keys_prefix", "key_prefix"),
    )


class BackgroundSession(Base):
    """A background session — one per user (mono) or many per user (multi).

    Each background session has its own agent context, memory, and routing keys.
    Triggers are routed to the correct session via routing_keys.
    """

    __tablename__ = "background_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("applications.app_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")  # active, paused, stopped

    # Custom params passed at session creation (e.g. CV text, preferences)
    params: Mapped[dict] = mapped_column(JSON, default=dict)

    # Routing keys: maps trigger_type → identifier
    # e.g. {"telegram": "chat_12345", "webhook": "user_abc"}
    routing_keys: Mapped[dict] = mapped_column(JSON, default=dict)

    # Workspace override (optional)
    workspace: Mapped[str] = mapped_column(String(1024), default="")

    # Timing
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Stats
    activation_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_bg_sessions_app_user", "app_id", "user_id"),
        Index("ix_bg_sessions_app_status", "app_id", "status"),
    )


class Activation(Base):
    """Record of a background trigger activation.

    Every time a background trigger fires and the agent runs, one row
    is created. Used for monitoring, debugging, and billing.
    """

    __tablename__ = "activations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("applications.app_id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger_id: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)  # cron, watch, http
    status: Mapped[str] = mapped_column(String(32), default="running")  # running, completed, failed, cancelled

    # Session & User (for multi-session background apps)
    session_id: Mapped[str] = mapped_column(String(64), default="")
    user_id: Mapped[str] = mapped_column(String(64), default="")

    # Input
    message: Mapped[str] = mapped_column(Text, default="")
    trigger_payload: Mapped[dict] = mapped_column(JSON, default=dict)

    # Timing
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)

    # Result
    response: Mapped[str] = mapped_column(Text, default="")
    tool_calls_count: Mapped[int] = mapped_column(Integer, default=0)
    turns_used: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Tokens
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)

    events: Mapped[list["ActivationEvent"]] = relationship(
        back_populates="activation",
        cascade="all, delete-orphan",
        order_by="ActivationEvent.sequence",
    )

    __table_args__ = (
        Index("ix_activations_app", "app_id"),
        Index("ix_activations_app_trigger", "app_id", "trigger_id"),
        Index("ix_activations_app_status", "app_id", "status"),
        Index("ix_activations_started", "started_at"),
    )


class ActivationEvent(Base):
    """Timeline event emitted during a background activation.

    Every tool call, thinking block, artifact creation and channel send
    that happens between an activation's ``started_at`` and
    ``completed_at`` is persisted here as a row, keyed by
    ``activation_id`` and ordered by ``sequence``. This is what the
    Flutter dashboard's activation drawer reads to render the
    ⟶ tool ⟶ thinking ⟶ artifact ⟶ channel timeline.

    Event types currently persisted by ``background.run_background``:

    - ``tool_call``   — a tool executed during the turn
                        (payload: name, params, success, duration_ms,
                        error, summary of the result)
    - ``thinking``    — a block of reasoning text from the model
                        (payload: text, truncated)
    - ``channel_sent``— a channel delivered a message
                        (payload: channel_name, target, success, error)
    - ``artifact``    — a file-producing tool call normalised to an
                        artifact row (payload: path, size_bytes, action)
    - ``turn_start``  — boundary marker so the drawer can split by turn
    - ``turn_end``    — boundary marker

    ``data`` carries the payload as JSON; its shape is event-specific
    but stable enough for the frontend to switch on ``event_type``.
    """

    __tablename__ = "activation_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    activation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("activations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Monotonically increasing per activation — the frontend sorts by
    # this field to guarantee a stable order even when multiple events
    # hit the database inside the same millisecond.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict)

    activation: Mapped["Activation"] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_activation_events_activation_seq", "activation_id", "sequence"),
        Index("ix_activation_events_type", "activation_id", "event_type"),
    )


class Credential(Base):
    """One encrypted credential — the foundation of the universal
    credentials/secrets/integrations system.

    A Credential is the unit of authentication for an external
    service. Its shape is intentionally open so the same row can
    represent:

    - a plain API key (``provider_type='api_key'``),
    - a multi-field credential like Slack (3 tokens) or AWS,
    - an OAuth 2 refresh + access token pair,
    - a database connection string,
    - an MCP server config (fields + env + command template).

    Every actual *field value* lives inside ``encrypted_fields`` as an
    AES-256-GCM ciphertext — nothing secret ever touches the database
    in plaintext. The ``display_metadata`` column carries the
    non-secret info needed by the UI (masked previews, OAuth account
    display name, scope list, etc.).

    Scope — **how the credential is resolved**::

        per_app_per_user   specific override : (user_id, app_id, provider)
        per_user           cross-app user secret : (user_id, NULL, provider)
        per_app_shared     rare: admin sets one key for all users of an app
        system_wide        daemon-level config (OAuth client_id, Twilio SID, …)

    A resolver walks this order from most specific to least specific
    at both compile time (for ``{{secret.X}}`` in YAML) and runtime
    (for per-user secrets inside rendered templates).

    Status lifecycle::

        pending   - created, fields not filled yet (OAuth flow started, …)
        filled    - fields present, not yet tested against the live service
        valid     - tested OK, safe to use
        expired   - OAuth token expired, refresh will be attempted
        invalid   - permanently broken (revoked, bad credentials)
        refreshing- in the middle of a refresh
        error     - last use raised an unknown error

    The refresh worker (background task) watches ``expires_at`` and
    tries to refresh credentials that are about to expire. For
    types that cannot be refreshed (api_key), the handler's ``refresh``
    implementation performs a ``test_live_connection`` instead and
    updates ``last_validated_at``.
    """

    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)

    # Scope identity — NULL means "not scoped on this axis"
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    app_id: Mapped[str | None] = mapped_column(
        String(255),
        # No FK: system_wide credentials have app_id=NULL and we don't
        # want cascading deletes from apps to wipe them.
        nullable=True,
    )
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)

    # Owner type — who owns this credential in the unified model.
    # "user"   → credential belongs to ``user_id``; apps access it via
    #            rows in ``credential_grants``.
    # "system" → admin/global credential, no grant needed. If ``app_id``
    #            is set, the credential is restricted to that one app.
    owner_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user",
    )
    # Human label for the picker UI ("personal", "work", ...). Defaults
    # to "default" — most users only have one credential per provider.
    label: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default",
    )

    # Encrypted blob — the actual field values
    encrypted_fields: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Lifecycle state
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Display metadata — NOT secret, stored as plain JSON for the UI.
    # Contains: masked_preview, oauth_account, oauth_scopes, label,
    # icon_url, mcp_status, …
    display_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    __table_args__ = (
        # At most one credential per (user?, app?, provider) tuple.
        # Note: SQLite treats NULLs as distinct in UNIQUE constraints,
        # so system_wide can't rely on this. The store enforces
        # uniqueness in the application layer as a safety net.
        Index("ix_credentials_user", "user_id"),
        Index("ix_credentials_app", "app_id"),
        Index("ix_credentials_user_app", "user_id", "app_id"),
        Index("ix_credentials_provider", "provider_name"),
        Index("ix_credentials_scope", "scope"),
        Index("ix_credentials_expires", "expires_at"),
        Index("ix_credentials_owner_type", "owner_type"),
        Index("ix_credentials_user_provider", "user_id", "provider_name"),
    )


class CredentialGrant(Base):
    """Authorization linking a user-owned credential to a specific app.

    In the unified credentials model a user stores a credential ONCE
    (``owner_type='user'``). Apps never "own" a credential — they are
    granted access to it. This table is the grant. One row per
    (credential_id, app_id) pair; the user can revoke by deleting the
    row (or by setting ``revoked_at`` — soft delete kept for audit).

    For ``oauth2`` credentials, ``scopes_granted`` records which OAuth
    scopes were requested when the grant was created so the runtime
    can detect when a new app asks for a superset and trigger an
    incremental scope upgrade.

    System credentials (``owner_type='system'``) never appear in this
    table — they are visible to every app implicitly (or restricted
    to the single app named in their own ``app_id`` column).
    """

    __tablename__ = "credential_grants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)

    credential_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("credentials.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # OAuth-specific: list of scopes that were actually granted when
    # this row was created. Stored as JSON array of strings. For
    # non-OAuth credentials this stays empty.
    scopes_granted: Mapped[list] = mapped_column(JSON, default=list)

    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        Index("ix_cred_grants_credential", "credential_id"),
        Index("ix_cred_grants_user", "user_id"),
        Index("ix_cred_grants_app", "app_id"),
        Index("ix_cred_grants_user_app", "user_id", "app_id"),
        Index("ix_cred_grants_lookup", "user_id", "app_id", "credential_id"),
    )


class BuildDraft(Base):
    """A work-in-progress YAML draft authored by the App Builder agent.

    Every conversation between a user and the App Builder is persisted
    as a ``BuildDraft`` so the user can leave, come back hours later,
    and pick up exactly where they left off — same chat history, same
    YAML in progress, same builder state-machine step.

    The actual YAML lives in two places (mirroring the
    ``BackgroundSession`` payload pattern): a copy in ``current_yaml``
    for fast DB lookup, and the same bytes on disk under
    ``~/.digitorn/drafts/<user_id>/<draft_id>/app.yaml`` so the user
    can ``cat`` / download it without going through the API.

    Each user is capped at 50 drafts (enforced by ``BuildDraftStore``)
    to keep the table from growing unbounded when users abandon
    experiments.
    """

    __tablename__ = "build_drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)

    name: Mapped[str] = mapped_column(String(255), default="Untitled draft")
    # in_progress | compiled | deployed | abandoned
    status: Mapped[str] = mapped_column(String(32), default="in_progress")

    # Current YAML in progress. Mirrored to ``yaml_path`` on disk by the
    # store so the bytes stay in sync between DB and filesystem.
    current_yaml: Mapped[str] = mapped_column(Text, default="")
    yaml_path: Mapped[str] = mapped_column(String(1024), default="")

    # Full chat history for the build session: a list of message dicts
    # with role/content + any structured build:* events the builder
    # emitted along the way. Capped to ~500 messages by the store.
    chat_history: Mapped[list] = mapped_column(JSON, default=list)

    # State-machine bookkeeping for the builder agent: current step,
    # collected user intent, picked template, last compile result, etc.
    # Free-form so we don't tie the schema to one builder version.
    builder_state: Mapped[dict] = mapped_column(JSON, default=dict)

    # If/when this draft is deployed, we record the resulting app_id so
    # the dashboard can link "draft → live app" both directions.
    deployed_app_id: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    __table_args__ = (
        Index("ix_build_drafts_user", "user_id"),
        Index("ix_build_drafts_user_status", "user_id", "status"),
        Index("ix_build_drafts_updated", "updated_at"),
    )


class InstalledPackage(Base):
    """One installed AppPackage — the unit of distribution.

    Created by the AppPackages system (``digitorn.core.packages``)
    when an app is installed via ``POST /api/packages/install`` or
    auto-installed by the daemon's BuiltinSource scan at boot. The
    row tracks **where the package came from**, **what version is
    installed**, **the content hash** (for drift detection), and
    **a frozen copy of the manifest** (for fast lookups without
    re-reading the TOML file).

    The deployed app side of the equation lives in ``Application``
    (with the matching ``package_id`` foreign-key-like column).
    Packages and applications are linked 1:1 — installing a package
    deploys exactly one app, uninstalling deletes it.

    Status lifecycle::

        installing  - install flow in progress, files being moved
        installed   - normal state, app is deployed
        broken      - install succeeded but the app failed to deploy;
                      the user can either uninstall or try to fix
        upgrading   - upgrade in progress, old version still live
        degraded    - new version deployed but is crashing at runtime
                      (rolled back manually by the user)
        uninstalling- uninstall in progress, files being removed
    """

    __tablename__ = "installed_packages"

    # Surrogate primary key — package_id alone is no longer unique
    # because a package can be installed at two scopes (system
    # install by admin AND a per-user override by Alice). The
    # uniqueness is enforced by the composite index below.
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)

    package_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Scoping — who can see this install.
    # ``system``: installed by admin, visible to every user of the
    #   daemon. Files live under ~/.digitorn/packages/<package_id>/.
    # ``user``: installed by a specific user, invisible to others.
    #   Files live under ~/.digitorn/users/<owner_user_id>/packages/<package_id>/.
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="system",
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Set when scope='user'; NULL when scope='system'",
    )

    # Source attribution — locked design D1
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # builtin | local | hub | git
    source_uri: Mapped[str] = mapped_column(String(1024), default="")
    # bundle://digitorn/builder            — for builtin
    # file:///abs/path/to/my-app           — for local
    # hub://alice/jobhunt@1.2.0            — for hub (future)
    # git+https://github.com/alice/...     — for git (future)

    version: Mapped[str] = mapped_column(String(32), default="0.0.0")
    hash: Mapped[str] = mapped_column(String(64), default="")
    install_dir: Mapped[str] = mapped_column(String(1024), default="")

    # Frozen copy of package.toml at install time. Used by API
    # listing routes so we don't re-read the TOML on every call.
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(16), default="installed")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )
    installed_by: Mapped[str] = mapped_column(
        String(64), default="",
        comment="user_id who triggered the install — NULL/empty for builtin",
    )

    __table_args__ = (
        Index("ix_installed_packages_source", "source_type"),
        Index("ix_installed_packages_status", "status"),
        Index("ix_installed_packages_updated", "updated_at"),
        Index("ix_installed_packages_package_id", "package_id"),
        Index("ix_installed_packages_owner", "owner_user_id"),
        # Uniqueness: one install per (package_id, scope, owner_user_id).
        # A user can have their own copy AND see the system copy at the
        # same time — scope differentiates them. SQLite treats NULL as
        # distinct in unique indices so system installs (owner_user_id=NULL)
        # all collide → we enforce uniqueness in the store layer with a
        # lookup before insert.
        Index(
            "ix_installed_packages_scope_key",
            "package_id", "scope", "owner_user_id",
            unique=True,
        ),
    )


class InboxItem(Base):
    """One notification in the user's persistent inbox.

    The inbox is the client's long-lived view of "things that
    happened while I wasn't looking". It survives reloads, is
    shared across devices, and is the canonical record the Flutter
    ``ActivityInboxService`` syncs from on launch.

    A row is created by the InboxProducer background task that
    subscribes to the per-user event fan-out and promotes specific
    events (session completed, approval requested, credential
    missing, …) into durable rows. Read / archive actions come
    from the /api/users/me/inbox/* routes.

    Kinds::

        session.completed           turn finished, no error
        session.failed              turn errored or hit an auth issue
        session.awaiting_approval   approval_request fired
        bg.activation_completed     background trigger ran + finished
        credential.expired          refresh worker flagged expiration
        credential.missing          CredentialAuthRequired propagated
        quota.warning               quota module >= warning threshold
    """

    __tablename__ = "inbox_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    subtitle: Mapped[str] = mapped_column(Text, default="")

    app_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credential_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)

    item_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        Index("ix_inbox_user", "user_id"),
        Index("ix_inbox_user_unread", "user_id", "read_at"),
        Index("ix_inbox_user_created", "user_id", "created_at"),
        Index("ix_inbox_app_session", "app_id", "session_id"),
    )


class InboxDevice(Base):
    """A registered device for future push-notification delivery.

    Persisted today so the Flutter client can wire register /
    unregister calls; actual FCM/APNS delivery is deferred to
    the push-notification PR.
    """

    __tablename__ = "inbox_devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    fcm_token: Mapped[str] = mapped_column(Text, nullable=False)
    device_name: Mapped[str] = mapped_column(String(128), default="")
    app_version: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    __table_args__ = (
        Index("ix_inbox_devices_user", "user_id"),
        Index("ix_inbox_devices_token", "fcm_token"),
    )


class UsageEvent(Base):
    """One row per LLM call — the foundation of token/cost accounting.

    Written by ``AppManager`` at the end of every turn (both
    interactive and background) so the daemon has a complete
    audit trail of who spent what, on which app, with which model,
    against which credential.

    The rows are append-only. Aggregation queries (monthly totals,
    by_app, by_model, 24h / 30d time series) run on demand via the
    ``UsageStore``.

    Aggregates are NOT pre-computed — SQLite is fast enough on the
    indices declared below for the scale we target (single-daemon
    multi-user, <1M rows/month). When we move past that, the store
    can transparently back the aggregates with a materialized view.
    """

    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(255), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Model identity — provider id + raw model string from the brain
    # config. "anthropic/claude-opus-4-6" is the canonical form.
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)

    # Tokens
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Monetary cost in USD, computed at write time from the model
    # price table. Kept as float rather than int(microcents) so the
    # aggregation SQL stays trivial.
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Optional link back to the credential that paid for this call.
    # When the user's own key was used, this is the credential_id;
    # when a system credential was used, it's the system id. Null
    # is acceptable (some legacy paths don't resolve through the
    # credential store).
    credential_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # When the call actually happened (end of turn). We query by
    # day/hour windows so a dedicated index on ``created_at`` is
    # important.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )

    __table_args__ = (
        Index("ix_usage_user_created", "user_id", "created_at"),
        Index("ix_usage_user_app_created", "user_id", "app_id", "created_at"),
        Index("ix_usage_user_model_created", "user_id", "model", "created_at"),
        Index("ix_usage_credential", "credential_id"),
        Index("ix_usage_created", "created_at"),
    )


class UserQuota(Base):
    """Admin- or user-set token quota for a scope + period.

    Scope options::

        scope_type=user      scope_id=<user_id>  app_id=NULL
            → cross-app user limit ("this user can't spend more
              than 1M tokens/month total, no matter which app")

        scope_type=user_app  scope_id=<user_id>  app_id=<app_id>
            → per-user-per-app limit ("alice can spend 500k/month
              on digitorn-code but 2M/month on digitorn-chat")

        scope_type=app       scope_id=<app_id>   app_id=<app_id>
            → shared across every user of the app ("digitorn-code
              total is 10M/month for the whole team")

    ``period`` = day | week | month. Enforcement walks the
    corresponding rolling window of usage_events and blocks new
    calls when over limit.

    ``set_by`` tracks the admin user who created the row for audit.
    NULL means the row was self-set by the user.
    """

    __tablename__ = "user_quotas"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    period: Mapped[str] = mapped_column(String(16), nullable=False, default="month")
    tokens_limit: Mapped[int] = mapped_column(Integer, nullable=False)

    set_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    __table_args__ = (
        Index("ix_quotas_scope", "scope_type", "scope_id"),
        Index("ix_quotas_scope_app", "scope_type", "scope_id", "app_id"),
    )


class InboxNotificationPrefs(Base):
    """Server-side mirror of the client's notification prefs.

    One row per user. Stored as JSON so the schema can evolve
    without migrations.
    """

    __tablename__ = "inbox_notification_prefs"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class SessionWorkspaceSnapshot(Base):
    """Durable snapshot of a session's preview / workspace state.

    The ``preview`` module keeps a per-session ``PreviewSessionState``
    in memory that carries everything the client needs to reproduce the
    current UI — scalar state map, resource channels (``files``,
    ``nodes``, ``edges``, ``slides``, …), and the last seq number. This
    table persists that state so:

    - Closing and reopening a session restores the exact same view
      (same files on screen, same React preview, same slide deck).
    - The daemon can restart without losing in-flight workspace state.
    - A session can be "forked" by cloning one row into a new
      ``session_id``.

    Writes are debounced (~500 ms) inside the preview module so a burst
    of mutations from the agent turns into a single row update.
    ``preview_seq`` lets clients issue ``since: N`` replays: anything
    received via Socket.IO after the snapshot was taken can be applied
    on top without duplication.

    ``snapshot_version`` is reserved for format migrations — increment
    it when the structure of ``state`` or ``resources`` changes
    incompatibly so clients can detect + handle the bump.
    """

    __tablename__ = "session_workspace_snapshots"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    app_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(64), default="", server_default="", nullable=False,
        comment="Owner of the session — empty for system/anonymous",
    )
    state: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False,
        comment="Scalar state map (set_state / patch_state)",
    )
    resources: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False,
        comment="Named channels — {files: {...}, nodes: {...}, edges: {...}, ...}",
    )
    preview_seq: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False,
        comment="Last seq emitted; client uses this for since-replay",
    )
    snapshot_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False,
        comment="Format version for forward-compat migrations",
    )
    saved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    __table_args__ = (
        Index("ix_workspace_snapshots_app", "app_id"),
    )


class SessionEvent(Base):
    """Persistent log of every event published on the session bus.

    Stored alongside the ring-buffer replay so the client can reconstruct
    the full turn timeline (hooks, tool calls, agent spawns, errors,
    message lifecycle, …) long after the in-memory ring has rolled over
    AND across daemon restarts. Enables "open a session from last year,
    see everything that happened" without relying on the transient
    event bus.

    Shape mirrors the Socket.IO envelope: ``type``, ``kind``,
    ``payload``, ``seq`` (per-user monotonic), ``ts``. The ``seq`` is
    the same the Socket.IO clients see — a reload uses
    ``GET /events?since=<seq>`` to backfill the gap between the
    ring-buffer window and the current state.

    Token-level events are NOT logged here (too noisy). They're
    reconstructed client-side from the persisted assistant message
    content. Everything else IS logged.
    """

    __tablename__ = "session_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="session", server_default="session", nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False, index=True)

    __table_args__ = (
        Index("ix_session_events_sid_seq", "session_id", "seq"),
        Index("ix_session_events_sid_ts", "session_id", "ts"),
        Index("ix_session_events_corr", "correlation_id"),
    )


class SessionMessageQueue(Base):
    """Per-session message queue — messages sent while a turn is running.

    When a client POSTs a new message to a session whose agent turn is
    already running, the daemon enqueues it here instead of failing with
    ``session_busy``. A per-session dispatcher pulls the head of the queue
    as soon as the running turn finishes, preserving FIFO order.

    Persistence survives daemon restart: at boot, the app manager
    rehydrates every session's queue from this table so queued work
    isn't lost. The table is also the source of truth for the
    ``GET /queue`` endpoint — the client sees exactly what's pending.

    ``status`` lifecycle:
        queued     → waiting for dispatch
        running    → currently being processed (one per session at a time)
        completed  → finished successfully; row kept briefly for GET /queue
        cancelled  → user clicked cancel (DELETE /queue/{id}) before it ran
        failed     → turn raised an exception OR expired via ttl

    ``correlation_id`` ties the row to SSE events:
        message_queued   → message_started → message_done / message_cancelled
    so the client can track a specific message from submission to result.
    """

    __tablename__ = "session_message_queue"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    image_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="queued", server_default="queued", nullable=False,
    )
    correlation_id: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    retries_remaining: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ttl_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)

    __table_args__ = (
        Index("ix_queue_session_status", "session_id", "status", "position"),
        Index("ix_queue_app_session", "app_id", "session_id"),
    )
