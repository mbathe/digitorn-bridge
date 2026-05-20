"""Digitorn - ORM models for persistence."""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Enum, FetchedValue, Float, ForeignKey, Index, Integer, JSON, LargeBinary, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from digitorn.core.database import Base


_JSON_X = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


from digitorn.core.unique_clock import unique_utc_now as _unique_utcnow  # noqa: E402


def _uuid() -> str:
    return uuid.uuid4().hex


class EncryptedJSON(TypeDecorator):
    """SQLAlchemy type that stores JSON data encrypted at rest."""

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
    """A registered application that connects to Digitorn."""

    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
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

    # Cached YAML for fallback when install_dir on disk is missing
    # (content-only deploys, or orphaned rows after manual cleanup).
    yaml_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    yaml_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    yaml_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    package_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Links to installed_packages.package_id when this app came from a package",
    )
    source_type: Mapped[str] = mapped_column(
        String(16), default="local",
        comment="local | builtin | hub | git - how this app was installed",
    )
    package_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="SHA-256 of the package content at install time, for drift detection",
    )

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

    hidden: Mapped[bool] = mapped_column(
        default=False, server_default="0", nullable=False,
        comment="True = app filtered out of non-admin lists. App stays deployed.",
    )
    hidden_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="When the app was hidden (UTC).",
    )

    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="application",
        primaryjoin="foreign(UserSession.app_id) == Application.app_id",
        viewonly=True,
    )
    security_profile: Mapped["AppProfile | None"] = relationship(
        back_populates="application",
        primaryjoin="foreign(AppProfile.app_id) == Application.app_id",
        uselist=False,
        viewonly=True,
    )
    module_configs: Mapped[list["AppModuleConfig"]] = relationship(
        back_populates="application",
        primaryjoin="foreign(AppModuleConfig.app_id) == Application.app_id",
        viewonly=True,
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


class AppProfile(Base):
    """Security profile for an application."""

    __tablename__ = "app_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
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

    application: Mapped["Application"] = relationship(
        back_populates="security_profile",
        primaryjoin="foreign(AppProfile.app_id) == Application.app_id",
        viewonly=True,
    )
    module_grants: Mapped[list["AppModuleGrant"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class AppModuleGrant(Base):
    """Per-module security configuration for an application."""

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
    """Per-module configuration and constraints from the app YAML."""

    __tablename__ = "app_module_configs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    module_id: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    application: Mapped["Application"] = relationship(
        back_populates="module_configs",
        primaryjoin="foreign(AppModuleConfig.app_id) == Application.app_id",
        viewonly=True,
    )

    __table_args__ = (
        Index("ix_app_module_configs_app_module", "app_id", "module_id", unique=True),
    )


class AppSecret(Base):
    """Encrypted secret for an application."""

    __tablename__ = "app_secrets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
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


class _UserRef(Base):
    """FK-target stub for the `users` table - schema mirrors the"""

    __tablename__ = "users"
    __table_args__ = {"extend_existing": True}

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    app_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(_JSON_X, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("TRUE"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserOAuthToken(Base):
    """OAuth2 tokens for a user."""

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

    __table_args__ = (
        Index("ix_oauth_tokens_user_provider", "user_id", "provider", unique=True),
    )


class UserSnippet(Base):
    """Per-user, per-app reusable prompt template the chat composer's"""

    __tablename__ = "user_snippets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    app_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    emoji: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(_JSON_X, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    __table_args__ = (
        # Composite index for the only query the CRUD path runs:
        # "list this user's snippets for this app".
        Index("ix_user_snippets_user_app", "user_id", "app_id"),
    )


class UserSkill(Base):
    """Per-user, per-app authored skill (system-prompt directive)."""

    __tablename__ = "user_skills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    app_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(
        String(300), nullable=False, default="",
    )
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    __table_args__ = (
        Index("ix_user_skills_user_app", "user_id", "app_id"),
        Index(
            "ux_user_skills_user_app_name",
            "user_id", "app_id", "name",
            unique=True,
        ),
    )


class UserSession(Base):
    """A user session within an application."""

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(
        "external_sid", String(255), nullable=False, index=True,
    )
    user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    workspace: Mapped[str] = mapped_column(String(1024), default="")
    workdir: Mapped[str] = mapped_column(String(1024), default="")

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active",
    )
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_completed_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("agent_runs.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )

    application: Mapped["Application"] = relationship(
        back_populates="sessions",
        primaryjoin="foreign(UserSession.app_id) == Application.app_id",
        viewonly=True,
    )
    agents: Mapped[list["SessionAgent"]] = relationship(back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_user_sessions_app_external_sid", "app_id", "external_sid", unique=True),
    )


class SessionAgent(Base):
    """A specialist registered for a user session."""

    __tablename__ = "session_agents"

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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    session: Mapped["UserSession"] = relationship(back_populates="agents")
    runs: Mapped[list["AgentRun"]] = relationship(
        back_populates="session_agent", cascade="all, delete-orphan",
    )
    executions: Mapped[list["ActionExecution"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_session_agents_session_agent", "session_pk", "agent_id", unique=True),
    )


Agent = SessionAgent


class AgentRun(Base):
    """One spawn / wait-for cycle of a SessionAgent."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)

    session_agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("session_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_pk: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("user_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Lifecycle
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued",
    )
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Inputs
    specialist: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fallback_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    task_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Budget
    max_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    turns_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    sub_agents_spawned: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # Usage counters
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )

    # Cost (per-provider breakdown; trigger materialises total_cost_usd)
    cost_breakdown: Mapped[dict[str, Any]] = mapped_column(
        _JSON_X, nullable=False, default=dict, server_default=text("'{}'"),
    )

    # Timing
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    total_tokens: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, server_default=FetchedValue(),
    )
    duration_ms: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, server_default=FetchedValue(),
    )
    total_cost_usd: Mapped[float | None] = mapped_column(
        Numeric(14, 6), nullable=True, server_default=FetchedValue(),
    )

    session_agent: Mapped["SessionAgent"] = relationship(back_populates="runs")
    events: Mapped[list["AgentRunEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan",
        order_by="AgentRunEvent.sequence",
    )


class AgentRunEvent(Base):
    """Append-only timeline event inside an AgentRun."""

    __tablename__ = "agent_run_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(
        _JSON_X, nullable=False, default=dict, server_default=text("'{}'"),
    )
    elapsed_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )

    run: Mapped["AgentRun"] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_run_events_run_sequence"),
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
        ForeignKey("session_agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    agent_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    agent: Mapped["SessionAgent | None"] = relationship(back_populates="executions")

    __table_args__ = (
        Index("ix_executions_app_module", "app_id", "module_id"),
        Index("ix_executions_app_session", "app_id", "session_id"),
    )


class SessionCheckpoint(Base):
    """Durable checkpoint of a session's execution state."""

    __tablename__ = "session_checkpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
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
    """An MCP server installed and managed at the daemon level."""

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
    """A role defines a set of permissions."""

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
    """Association between users and roles."""

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
        nullable=True,
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    granted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    role: Mapped["Role"] = relationship(back_populates="user_roles")

    __table_args__ = (
        Index("ix_user_roles_user_app", "user_id", "app_id"),
        Index("ix_user_roles_unique", "user_id", "role_id", "app_id", unique=True),
    )


class HistoryLog(Base):
    """Unified bank-grade ledger - messages, events, admin actions."""
    __tablename__ = "history_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # `ts` is the **globally unique** ordering key. UNIQUE + INDEX.
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_unique_utcnow,
        nullable=False, unique=True, index=True,
    )
    # `seq` is monotonic within a session (used for pagination +
    # ring-buffer replay). Not unique across sessions.
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)

    # Coarse kind so readers can filter at index-scan speed.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # Fine type, e.g. "user_message", "tool_call", "thinking_delta",
    # "quota.set_app", "user.disable".
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    app_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    before: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    after: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    target_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    target_app_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    target_resource: Mapped[str | None] = mapped_column(String(255), nullable=True)

    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)

    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (
        # Common access patterns
        Index("ix_history_session_ts", "session_id", "ts"),
        Index("ix_history_session_seq", "session_id", "seq"),
        Index("ix_history_app_ts", "app_id", "ts"),
        Index("ix_history_user_ts", "user_id", "ts"),
        Index("ix_history_actor_ts", "actor_user_id", "ts"),
        Index("ix_history_kind_ts", "kind", "ts"),
        Index("ix_history_type_ts", "type", "ts"),
        Index(
            "ix_history_session_seq_event_unique",
            "session_id", "seq",
            unique=True,
            sqlite_where=text("kind = 'event' AND session_id IS NOT NULL"),
            postgresql_where=text("kind = 'event' AND session_id IS NOT NULL"),
        ),
        Index(
            "ix_history_user_seq_event_unique",
            "user_id", "seq",
            unique=True,
            sqlite_where=text("kind = 'event' AND session_id IS NULL"),
            postgresql_where=text("kind = 'event' AND session_id IS NULL"),
        ),
    )


class RefreshToken(Base):
    """Stored refresh tokens - revocable, trackable."""

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
    """API keys for machine-to-machine authentication."""

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
    """A background session - one per user (mono) or many per user (multi)."""

    __tablename__ = "background_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
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
    """Record of a background trigger activation."""

    __tablename__ = "activations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
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
    """Timeline event emitted during a background activation."""

    __tablename__ = "activation_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    activation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("activations.id", ondelete="CASCADE"),
        nullable=False,
    )
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
    """One encrypted credential - the foundation of the universal"""

    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)

    # Scope identity - NULL means "not scoped on this axis"
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

    name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="",
    )

    owner_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user",
    )
    # Human label for the picker UI ("personal", "work", ...). Defaults
    # to "default" - most users only have one credential per provider.
    label: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default",
    )

    # Encrypted blob - the actual field values
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

    display_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    __table_args__ = (
        Index("ix_credentials_user", "user_id"),
        Index("ix_credentials_app", "app_id"),
        Index("ix_credentials_user_app", "user_id", "app_id"),
        Index("ix_credentials_provider", "provider_name"),
        Index("ix_credentials_scope", "scope"),
        Index("ix_credentials_expires", "expires_at"),
        Index("ix_credentials_owner_type", "owner_type"),
        Index("ix_credentials_user_provider", "user_id", "provider_name"),
        Index("ix_credentials_user_name", "user_id", "name"),
        Index("ix_credentials_scope_name", "scope", "name"),
    )


class CredentialGrant(Base):
    """Authorization linking a user-owned credential to a specific app."""

    __tablename__ = "credential_grants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)

    credential_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("credentials.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(255), nullable=False)

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


class CredentialAudit(Base):
    """Append-only audit ledger for every credential operation."""

    __tablename__ = "credential_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # WHO: user_id of the actor. "system" for daemon-initiated actions
    # (e.g. background OAuth refresh job).
    who: Mapped[str] = mapped_column(String(64), nullable=False)

    # WHAT: AuditAction enum value (string).
    action: Mapped[str] = mapped_column(String(32), nullable=False)

    when_ts: Mapped[float] = mapped_column(Float, nullable=False)

    # ON: the credential id targeted, or "*" for list operations.
    target: Mapped[str] = mapped_column(String(64), nullable=False)

    # OUTCOME: success / failure / denied
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # daemon-internal calls).
    where_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    where_ua: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # APP context, when the action is app-scoped
    # (resolution / injection / app-shared write).
    app_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Action-specific JSON. Must NEVER contain secrets - the audit
    # writer scrubs through LogScrubber before INSERT as belt-and-braces.
    extra: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Hash chain. SHA-256 hex, 64 chars each. Genesis prev_hash is "0"*64.
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    this_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (
        Index("ix_audit_target_id", "target", "id"),
        Index("ix_audit_who_id", "who", "id"),
        Index("ix_audit_app_id", "app_id"),
        Index("ix_audit_action_when", "action", "when_ts"),
        Index("ix_audit_outcome", "outcome"),
    )


class UserAppByok(Base):
    """Per-user, per-app "Bring Your Own Key" toggle (LOCAL mode only)."""

    __tablename__ = "user_app_byok"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    __table_args__ = (
        Index("ix_user_app_byok_user", "user_id"),
        Index("ix_user_app_byok_app", "app_id"),
    )


class BuildDraft(Base):
    """A work-in-progress YAML draft authored by the App Builder agent."""

    __tablename__ = "build_drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)

    name: Mapped[str] = mapped_column(String(255), default="Untitled draft")
    # in_progress | compiled | deployed | abandoned
    status: Mapped[str] = mapped_column(String(32), default="in_progress")

    # Current YAML in progress. Mirrored to `yaml_path` on disk by the
    # store so the bytes stay in sync between DB and filesystem.
    current_yaml: Mapped[str] = mapped_column(Text, default="")
    yaml_path: Mapped[str] = mapped_column(String(1024), default="")

    chat_history: Mapped[list] = mapped_column(JSON, default=list)

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
    """One installed AppPackage - the unit of distribution."""

    __tablename__ = "installed_packages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)

    package_id: Mapped[str] = mapped_column(String(64), nullable=False)

    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="system",
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Set when scope='user'; NULL when scope='system'",
    )

    # Source attribution - locked design D1
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # builtin | local | hub | git
    source_uri: Mapped[str] = mapped_column(String(1024), default="")

    version: Mapped[str] = mapped_column(String(32), default="0.0.0")
    hash: Mapped[str] = mapped_column(String(64), default="")

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
        comment="user_id who triggered the install - NULL/empty for builtin",
    )

    __table_args__ = (
        Index("ix_installed_packages_source", "source_type"),
        Index("ix_installed_packages_status", "status"),
        Index("ix_installed_packages_updated", "updated_at"),
        Index("ix_installed_packages_package_id", "package_id"),
        Index("ix_installed_packages_owner", "owner_user_id"),
        Index(
            "ix_installed_packages_scope_key",
            "package_id", "scope", "owner_user_id",
            unique=True,
        ),
    )


class InboxItem(Base):
    """One notification in the user's persistent inbox."""

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
    """A registered device for future push-notification delivery."""

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


class InboxNotificationPrefs(Base):
    """Server-side mirror of the client's notification prefs."""

    __tablename__ = "inbox_notification_prefs"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class HubSession(Base):
    """One row per (daemon user, hub URL) - caches the hub JWT for that user."""

    __tablename__ = "hub_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    daemon_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    hub_url: Mapped[str] = mapped_column(String(512), nullable=False)
    hub_user_email: Mapped[str] = mapped_column(String(254), nullable=False)
    hub_user_id: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_hub_sessions_user_url", "daemon_user_id", "hub_url", unique=True),
    )


class UserDevice(Base):
    """A registered device with per-device notification preferences."""

    __tablename__ = "user_devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    device_name: Mapped[str] = mapped_column(String(128), default="", server_default="")
    app_version: Mapped[str] = mapped_column(String(32), default="", server_default="")
    fcm_token: Mapped[str] = mapped_column(Text, nullable=False)
    prefs: Mapped[dict[str, Any]] = mapped_column(
        _JSON_X, nullable=False, default=dict, server_default=text("'{}'"),
    )
    subscribed_topics: Mapped[list[str]] = mapped_column(
        _JSON_X, nullable=False, default=list, server_default=text("'[]'"),
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "fcm_token", name="uq_user_devices_user_token"),
        Index("ix_user_devices_active_seen", "active", "last_seen_at"),
    )


class FeatureFlag(Base):
    """Runtime feature gating with deterministic % rollout."""

    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="global", server_default="global",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    rollout_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    conditions: Mapped[dict[str, Any]] = mapped_column(
        _JSON_X, nullable=False, default=dict, server_default=text("'{}'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "rollout_percent >= 0 AND rollout_percent <= 100",
            name="ck_feature_flags_rollout_percent",
        ),
    )


class AuditActionsCatalog(Base):
    """Catalog of every audit `action_key` we emit, with retention."""

    __tablename__ = "audit_actions_catalog"

    action_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="info", server_default="info",
    )
    retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=365, server_default="365",
    )
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="other", server_default="other",
    )
    ui_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow,
    )
