"""Digitorn - ORM models for persistence.

Tables follow the isolation hierarchy (post v2 schema):

    Application → UserSession → SessionAgent → AgentRun → ActionExecution
                                                       └→ AgentRunEvent

A `SessionAgent` is one specialist per session. An `AgentRun` is one
spawn / wait-for cycle of that specialist (queued → active → terminal).
`AgentRunEvent` is the append-only timeline within a run. Tool calls
land in `ActionExecution` linked to the parent `AgentRun`.
"""

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


# Cross-dialect JSON type. JSONB on Postgres (GIN index, native ops),
# plain JSON (text-backed) on SQLite for self-hosted local runtimes.
# Using ``with_variant`` keeps the model declaration single-sourced
# while ``Base.metadata.create_all`` produces the right DDL for each
# dialect. Migration 0002 still ALTERs JSON->JSONB on Postgres for any
# legacy column that landed via create_all before this typing change.
_JSON_X = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Strictly-monotonic UTC clock - used as the default for every
# history/audit column that MUST carry a unique timestamp. Collision
# under burst writes is avoided in-process; cross-process collisions
# are caught by the DB UNIQUE constraint and handled by the caller
# via ``unique_ts_retry`` (see ``digitorn.core.unique_clock``).
from digitorn.core.unique_clock import unique_utc_now as _unique_utcnow  # noqa: E402


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

    A deployed app lives on disk at ``~/.digitorn/apps/<scoped>/`` -
    the install dir IS the source of truth. The daemon reloads each
    app by recompiling that dir at startup. ``yaml_content`` on this
    row is kept as a fallback for content-only deploys whose install
    dir is missing.
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

    # Cached YAML for fallback when install_dir on disk is missing
    # (content-only deploys, or orphaned rows after manual cleanup).
    yaml_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    yaml_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    yaml_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
        comment="local | builtin | hub | git - how this app was installed",
    )
    package_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="SHA-256 of the package content at install time, for drift detection",
    )

    # ── Disable support ────────────────────────────────────────
    # When `disabled=True`, the app is NOT reloaded at daemon startup, is
    # hidden from list_apps/get for non-admins, and session creation is
    # refused. Only an admin can re-enable (via POST /api/apps/{id}/enable).
    # History + sessions are preserved - disable is reversible.
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

    # ── Hidden support (visual only, app stays running) ──────────
    # When `hidden=True`, the app is filtered out of list_apps for
    # non-admin callers (respecting scope: a system-scope hide excludes
    # everyone, a user-scope hide excludes that user only). UNLIKE
    # ``disabled``, the app stays DEPLOYED and FUNCTIONAL - hide is
    # cosmetic. Used to declutter the user-facing dashboard while
    # keeping the app reachable for admin or programmatic flows (e.g.
    # the in-chat ``digitorn-builder`` is needed for deploy-from-chat
    # but doesn't have to clutter every user's app list). Reversible
    # via POST /api/apps/{id}/show.
    hidden: Mapped[bool] = mapped_column(
        default=False, server_default="0", nullable=False,
        comment="True = app filtered out of non-admin lists. App stays deployed.",
    )
    hidden_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="When the app was hidden (UTC).",
    )

    # Cross-table relationships joined on ``app_id``. There is no
    # DB-level FK between these children and ``applications`` because
    # ``applications.app_id`` is composite-unique (see __table_args__),
    # not unique alone. ``primaryjoin`` + ``foreign()`` tells the ORM
    # how to link without requiring a FK. ``viewonly=True`` on these
    # reverse collections because cascade delete is handled explicitly
    # at the application layer (``manager.delete_app``) rather than by
    # SQLA cascade, which can't traverse a non-FK join safely.
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
        # No DB-level FK: ``applications.app_id`` is not unique on its own
        # (uniqueness is composite ``(app_id, scope, owner_user_id)`` for
        # multi-tenant support). Postgres rejects FKs to non-unique columns;
        # SQLite silently accepted them but it was never valid. Cascade
        # cleanup is handled at the application layer (``delete_app`` /
        # ``disable_app``).
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
    of truth - this table is rebuilt on every sync.
    """

    __tablename__ = "app_module_configs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        # No DB-level FK: ``applications.app_id`` is not unique on its own
        # (uniqueness is composite ``(app_id, scope, owner_user_id)`` for
        # multi-tenant support). Postgres rejects FKs to non-unique columns;
        # SQLite silently accepted them but it was never valid. Cascade
        # cleanup is handled at the application layer (``delete_app`` /
        # ``disable_app``).
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
    """Encrypted secret for an application.

    Secrets are stored encrypted at rest using Fernet symmetric encryption
    (same key as OAuth tokens).  They are resolved via ``{{secret.KEY}}``
    in app YAML and injected at compile time.
    """

    __tablename__ = "app_secrets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        # No DB-level FK: ``applications.app_id`` is not unique on its own
        # (uniqueness is composite ``(app_id, scope, owner_user_id)`` for
        # multi-tenant support). Postgres rejects FKs to non-unique columns;
        # SQLite silently accepted them but it was never valid. Cascade
        # cleanup is handled at the application layer (``delete_app`` /
        # ``disable_app``).
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


# The ``users`` table is owned by the central digitorn-auth service.
# The daemon reads identity by calling GET /auth/me on that service
# and references users by their opaque ``user_id`` (the JWT ``sub``
# claim).
#
# We keep a tiny stub class here ONLY so SQLAlchemy can resolve the
# ``ForeignKey("users.id")`` declarations that downstream tables
# (user_oauth_tokens, user_sessions, user_roles, api_keys, …) still
# carry for DB-level referential integrity. The class has no fields
# beyond the PK, no relationships, and is never queried by the
# daemon. Reading user identity from this stub is intentionally
# impossible - if you find yourself wanting to, call the auth
# service via httpx instead.


class _UserRef(Base):
    """FK-target stub for the ``users`` table - schema mirrors the
    auth-service-owned ``digitorn_auth.models.User``.

    On Postgres prod the auth service owns the table and creates it
    with the full schema first; ``create_all`` is a no-op and
    ``_migrate_missing_columns`` has nothing to add. On SQLite local
    (self-hosted daemon, no separate auth service), the daemon owns
    the table - declaring the full schema here lets the JIT mirror
    (``digitorn_auth.fastapi``) INSERT into a complete table instead
    of failing on missing columns.

    Do NOT query this class from daemon code. Do NOT attach
    relationships to it. The auth service is the source of truth.
    """

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

    __table_args__ = (
        Index("ix_oauth_tokens_user_provider", "user_id", "provider", unique=True),
    )


class UserSnippet(Base):
    """Per-user, per-app reusable prompt template the chat composer's
    "Insert snippet" menu hands the user.

    Scoped on (``user_id``, ``app_id``) so the snippets the user
    builds while talking to one app don't bleed into another. The CRUD endpoints under
    ``/api/apps/{app_id}/snippets`` filter on the calling user's id
    transparently.

    ``body`` may contain ``{{variable}}`` placeholders the composer
    cycles through with Tab. Sanitisation lives at insertion-time
    in the composer; the daemon stores the body verbatim.
    """

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
    """Per-user, per-app authored skill (system-prompt directive).

    Gated behind ``dev.allow_user_skills: true`` in the app YAML.
    When the user sends ``/use_skill <name> <prompt>``, the daemon
    looks up the row by ``(user_id, app_id, name)``, strips the
    prefix from the message, and injects ``instructions`` as a
    turn-scoped ``role: system`` message (same mechanism as
    ``template_id``) so the agent must follow it for that turn only.

    Distinct from ``app_skills`` declared in ``dev.skills`` of the
    YAML: those are author-time, .md-backed, agent-callable via the
    ``use_skill`` tool; these are user-time, DB-backed, user-callable
    via the ``/use_skill`` composer command.
    """

    __tablename__ = "user_skills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    app_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Short slug used as both the picker label and the ``/use_skill <name>``
    # lookup key. Lowercase letters, digits, hyphens; the API rejects
    # anything else.
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(
        String(300), nullable=False, default="",
    )
    # Markdown body. Becomes the turn-scoped system prompt verbatim;
    # the agent loop is expected to wrap it in a leading "MANDATORY"
    # framing line so the LLM treats it as an authoritative directive.
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    __table_args__ = (
        Index("ix_user_skills_user_app", "user_id", "app_id"),
        # Unique per (user, app, name) so the `/use_skill <name>` lookup
        # is unambiguous. Two users CAN share a name; two apps for the
        # same user CAN share a name; same (user, app) pair cannot.
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
        # No DB-level FK: ``applications.app_id`` is not unique on its own
        # (uniqueness is composite ``(app_id, scope, owner_user_id)`` for
        # multi-tenant support). Postgres rejects FKs to non-unique columns;
        # SQLite silently accepted them but it was never valid. Cascade
        # cleanup is handled at the application layer (``delete_app`` /
        # ``disable_app``).
        nullable=False,
    )
    # Python attribute stays ``session_id`` (every call site reads
    # ``UserSession.session_id``) but the DB column is ``external_sid``
    # post v2 - same opaque client-supplied key, clearer name.
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

    # ── Sprint D additions ─────────────────────────────────────
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
    """A specialist registered for a user session.

    Renamed from ``Agent`` (table ``agents``) in v2. One row per
    (session, agent_id) pair: the same specialist key registered
    twice in a session reuses the same row. Each individual run /
    spawn of this specialist is recorded in ``agent_runs``.
    """

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


# Backwards-compat alias - some legacy imports still reference ``Agent``.
# Prefer ``SessionAgent`` going forward; this alias will be removed
# once every call site is updated.
Agent = SessionAgent


class AgentRun(Base):
    """One spawn / wait-for cycle of a SessionAgent.

    Lifecycle: queued → active → (completed | failed | cancelled |
    timeout | paused). All token / cost / turn counters live here so
    the dashboard can answer "what's running now?" and "who spent
    the most this week?" with one query against this table.

    Generated columns (Postgres 12+):
        * total_tokens   = prompt + completion + cache_read + cache_write
        * duration_ms    = completed_at - started_at, ms

    Trigger-materialised:
        * total_cost_usd = SUM(cost_breakdown[*].total_usd)
    """

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

    # Computed columns - the DB owns the value (GENERATED ALWAYS for
    # total_tokens / duration_ms, trigger-materialised for
    # total_cost_usd). FetchedValue() tells SQLAlchemy NOT to include
    # the column in INSERTs; it'll be read back after.
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
    """Append-only timeline event inside an AgentRun.

    ``sequence`` is per-run monotonic (starts at 1). ``event_type``
    is one of: lifecycle | turn | llm | tool | sub_agent | compaction
    | streaming. The ``data`` JSONB carries the event-specific payload.
    """

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
    """Durable checkpoint of a session's execution state.

    Saved after each agent turn. Contains everything needed to resume
    the session exactly where it left off after a daemon restart.
    """

    __tablename__ = "session_checkpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        # No DB-level FK: ``applications.app_id`` is not unique on its own
        # (uniqueness is composite ``(app_id, scope, owner_user_id)`` for
        # multi-tenant support). Postgres rejects FKs to non-unique columns;
        # SQLite silently accepted them but it was never valid. Cascade
        # cleanup is handled at the application layer (``delete_app`` /
        # ``disable_app``).
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

    MCP servers are first-class daemon resources - they must be installed
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
        # No DB-level FK: ``applications.app_id`` is not unique on its own
        # (uniqueness is composite ``(app_id, scope, owner_user_id)`` for
        # multi-tenant support). Postgres rejects FKs to non-unique columns;
        # SQLite silently accepted them but it was never valid. Cascade
        # cleanup is handled at the application layer (``delete_app`` /
        # ``disable_app``).
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
    """Unified bank-grade ledger - messages, events, admin actions.

    One table to read "everything that ever happened". Supersedes the
    earlier triplet (``session_messages`` + ``session_events`` +
    ``audit_log``). Those 3 stay around for a transition window during
    which we dual-write for safety; ``history_log`` is authoritative
    on the read path.

    Columns carry enough shape to express all three kinds:

      - **message**  - a turn exchange. ``role`` / ``content`` /
                         ``tool_calls`` set; ``payload`` carries usage
                         metadata (tokens, cost, …).
      - **event**    - any event the session bus emitted (tokens,
                         thinking, tool_start, compaction, hook,
                         quota_exceeded, approval_request, …).
                         ``type`` carries the envelope type;
                         ``payload`` the full envelope.
      - **audit**    - an admin action (quota change, user disable,
                         app deploy, …). ``actor_user_id``/``actor_roles``
                         set; ``before``/``after`` JSON snapshots;
                         ``ip_address``/``user_agent`` forensic fields.

    Ordering:

      - ``ts`` is UNIQUE - globally monotonic via the process-wide
        ``unique_utc_now`` clock. Collision under burst is avoided
        pre-insert; the UNIQUE constraint is a belt for multi-process.
      - ``seq`` is a per-session monotonic counter preserved from the
        legacy schema for client-side pagination.

    Every query is indexed. Common patterns:

      - Load full chronology of a chat:
        ``WHERE session_id = ? ORDER BY ts``
      - Show tool-call timeline: add ``AND type LIKE 'tool_%'``
      - Audit report for an admin:
        ``WHERE actor_user_id = ? AND kind = 'audit' ORDER BY ts``
      - Compliance export by time window: ``WHERE ts BETWEEN ? AND ?``
    """
    __tablename__ = "history_log"

    # ── Identity ────────────────────────────────────────────────
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ── Ordering keys ───────────────────────────────────────────
    # ``ts`` is the **globally unique** ordering key. UNIQUE + INDEX.
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_unique_utcnow,
        nullable=False, unique=True, index=True,
    )
    # ``seq`` is monotonic within a session (used for pagination +
    # ring-buffer replay). Not unique across sessions.
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)

    # ── Classification ──────────────────────────────────────────
    # Coarse kind so readers can filter at index-scan speed.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # Fine type, e.g. "user_message", "tool_call", "thinking_delta",
    # "quota.set_app", "user.disable".
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # ── Scoping (nullable) ──────────────────────────────────────
    app_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # ── Actor (who performed the action) ────────────────────────
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    # ── Message-shape fields (populated when kind='message') ───
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── Generic payload (events + full audit body) ─────────────
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # ── Audit-specific (populated when kind='audit') ───────────
    before: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    after: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    target_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    target_app_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    target_resource: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── Forensic (IP / UA / correlation) ───────────────────────
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)

    # ── Outcome ─────────────────────────────────────────────────
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
        # Universal-truth invariant for the seq column: per-session
        # monotonicity is enforced at the DB level so any code path
        # that mints a duplicate seq fails loudly instead of silently
        # corrupting the timeline. Partial index keyed on kind='event'
        # (audit / message rows are allowed to share seq=0). Two
        # variants - one for session-scoped events, one for user-scoped
        # events - mirror the in-memory scope key in
        # ``EventBuffer.next_seq``. Supported on SQLite >= 3.8.0 and
        # Postgres - both backends the daemon targets.
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
    """Stored refresh tokens - revocable, trackable.

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
        # No DB-level FK: ``applications.app_id`` is not unique on its own
        # (uniqueness is composite ``(app_id, scope, owner_user_id)`` for
        # multi-tenant support). Postgres rejects FKs to non-unique columns;
        # SQLite silently accepted them but it was never valid. Cascade
        # cleanup is handled at the application layer (``delete_app`` /
        # ``disable_app``).
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
    """A background session - one per user (mono) or many per user (multi).

    Each background session has its own agent context, memory, and routing keys.
    Triggers are routed to the correct session via routing_keys.
    """

    __tablename__ = "background_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(
        String(255),
        # No DB-level FK: ``applications.app_id`` is not unique on its own
        # (uniqueness is composite ``(app_id, scope, owner_user_id)`` for
        # multi-tenant support). Postgres rejects FKs to non-unique columns;
        # SQLite silently accepted them but it was never valid. Cascade
        # cleanup is handled at the application layer (``delete_app`` /
        # ``disable_app``).
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
        # No DB-level FK: ``applications.app_id`` is not unique on its own
        # (uniqueness is composite ``(app_id, scope, owner_user_id)`` for
        # multi-tenant support). Postgres rejects FKs to non-unique columns;
        # SQLite silently accepted them but it was never valid. Cascade
        # cleanup is handled at the application layer (``delete_app`` /
        # ``disable_app``).
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

    - ``tool_call``   - a tool executed during the turn
                        (payload: name, params, success, duration_ms,
                        error, summary of the result)
    - ``thinking``    - a block of reasoning text from the model
                        (payload: text, truncated)
    - ``channel_sent``- a channel delivered a message
                        (payload: channel_name, target, success, error)
    - ``artifact``    - a file-producing tool call normalised to an
                        artifact row (payload: path, size_bytes, action)
    - ``turn_start``  - boundary marker so the drawer can split by turn
    - ``turn_end``    - boundary marker

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
    # Monotonically increasing per activation - the frontend sorts by
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
    """One encrypted credential - the foundation of the universal
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
    AES-256-GCM ciphertext - nothing secret ever touches the database
    in plaintext. The ``display_metadata`` column carries the
    non-secret info needed by the UI (masked previews, OAuth account
    display name, scope list, etc.).

    Scope - **how the credential is resolved**::

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

    # User-facing slug used by YAML `credential: <name>` references.
    # Distinct from `provider_name` (e.g. name="openai_main",
    # provider_name="openai"). Unique per (scope, user_id, app_id) -
    # the same name can exist at different scopes intentionally.
    # Defaults to provider_name + label for back-compat with rows
    # written before this field existed.
    name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="",
    )

    # Owner type - who owns this credential in the unified model.
    # "user"   → credential belongs to ``user_id``; apps access it via
    #            rows in ``credential_grants``.
    # "system" → admin/global credential, no grant needed. If ``app_id``
    #            is set, the credential is restricted to that one app.
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

    # Display metadata - NOT secret, stored as plain JSON for the UI.
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
        # Lookup by user-facing slug (the YAML `credential: <name>` ref).
        # Filter by scope at the SQL layer so the strict-scope resolver
        # is a single round-trip.
        Index("ix_credentials_user_name", "user_id", "name"),
        Index("ix_credentials_scope_name", "scope", "name"),
    )


class CredentialGrant(Base):
    """Authorization linking a user-owned credential to a specific app.

    In the unified credentials model a user stores a credential ONCE
    (``owner_type='user'``). Apps never "own" a credential - they are
    granted access to it. This table is the grant. One row per
    (credential_id, app_id) pair; the user can revoke by deleting the
    row (or by setting ``revoked_at`` - soft delete kept for audit).

    For ``oauth2`` credentials, ``scopes_granted`` records which OAuth
    scopes were requested when the grant was created so the runtime
    can detect when a new app asks for a superset and trigger an
    incremental scope upgrade.

    System credentials (``owner_type='system'``) never appear in this
    table - they are visible to every app implicitly (or restricted
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


class CredentialAudit(Base):
    """Append-only audit ledger for every credential operation.

    Each row carries `prev_hash` + `this_hash` so the chain can be
    verified end-to-end. A row that's tampered with (or one that's
    deleted) breaks every subsequent `prev_hash` link, surfacing the
    breach to a periodic verifier.

    Hash construction:
        this_hash = SHA-256(prev_hash || canonical_json(this_row_fields))

    The genesis row uses `prev_hash = "0" * 64`. Persisted hashes are
    hex-encoded SHA-256 (64 chars).

    Operational notes:
      - Inserted under `with_for_update` lock on the chain head to
        serialise concurrent writers.
      - NEVER updated. The verifier tolerates a missing row only at
        the chain TAIL (truncated log) - any inner gap is a breach.
      - `extra` column carries action-specific small JSON details,
        scrubbed of secrets via the LogScrubber path before insert.
      - Retention is policy-driven (default: keep forever; admin can
        archive to cold storage via export).
    """

    __tablename__ = "credential_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # WHO: user_id of the actor. "system" for daemon-initiated actions
    # (e.g. background OAuth refresh job).
    who: Mapped[str] = mapped_column(String(64), nullable=False)

    # WHAT: AuditAction enum value (string).
    action: Mapped[str] = mapped_column(String(32), nullable=False)

    # WHEN: unix timestamp at the moment the action was attempted.
    # Not the row insert time - the action time. Useful when the audit
    # write itself is delayed by retries.
    when_ts: Mapped[float] = mapped_column(Float, nullable=False)

    # ON: the credential id targeted, or "*" for list operations.
    target: Mapped[str] = mapped_column(String(64), nullable=False)

    # OUTCOME: success / failure / denied
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # WHERE: client metadata (best-effort; may be empty for
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
    """Per-user, per-app "Bring Your Own Key" toggle (LOCAL mode only).

    A row exists when the user has flipped the BYOK switch for one of
    their installed apps from the Flutter desktop UI. Presence with
    ``enabled=True`` instructs the runtime to:

      1. Skip the Digitorn LLM gateway for that (user, app).
      2. Use the user's own credential (a ``Credential`` row with scope
         ``per_app_per_user``) when calling the LLM provider.
      3. If no such credential exists yet, raise ``CredentialAuthRequired``
         so the client opens the credential-picker dialog.

    The table is **never consulted in cloud mode**. The cloud routing
    layer always sends traffic through the gateway with the user's JWT;
    this toggle is meaningful only for self-hosted / desktop daemons.

    Why a separate table (not a column on ``credentials``):
      * The toggle precedes the credential. A user can flip BYOK on
        BEFORE they have a credential to inject - the picker is what
        fills the credentials row.
      * Rolling BYOK off should NOT delete the saved credential, so
        users can re-enable later without re-entering their key.
      * Conceptually it's an app-install setting (per (user, app)),
        not a credential field.
    """

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
    """A work-in-progress YAML draft authored by the App Builder agent.

    Every conversation between a user and the App Builder is persisted
    as a ``BuildDraft`` so the user can leave, come back hours later,
    and pick up exactly where they left off - same chat history, same
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
    """One installed AppPackage - the unit of distribution.

    Created by the AppPackages system (``digitorn.core.packages``)
    when an app is installed via ``POST /api/packages/install`` or
    auto-installed by the daemon's BuiltinSource scan at boot. The
    row tracks **where the package came from**, **what version is
    installed**, **the content hash** (for drift detection), and
    **a frozen copy of the manifest** (for fast lookups without
    re-reading the TOML file).

    The deployed app side of the equation lives in ``Application``
    (with the matching ``package_id`` foreign-key-like column).
    Packages and applications are linked 1:1 - installing a package
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

    # Surrogate primary key - package_id alone is no longer unique
    # because a package can be installed at two scopes (system
    # install by admin AND a per-user override by Alice). The
    # uniqueness is enforced by the composite index below.
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)

    package_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Scoping - who can see this install.
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

    # Source attribution - locked design D1
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # builtin | local | hub | git
    source_uri: Mapped[str] = mapped_column(String(1024), default="")
    # bundle://digitorn/builder            - for builtin
    # file:///abs/path/to/my-app           - for local
    # hub://alice/jobhunt@1.2.0            - for hub (future)
    # git+https://github.com/alice/...     - for git (future)

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
        # Uniqueness: one install per (package_id, scope, owner_user_id).
        # A user can have their own copy AND see the system copy at the
        # same time - scope differentiates them. SQLite treats NULL as
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



class HubSession(Base):
    """One row per (daemon user, hub URL) - caches the hub JWT for that user.

    The daemon never stores a hub password. The user logs into the hub via
    the daemon (`POST /api/hub/login`), the daemon forwards credentials to
    the hub, and the returned JWT is cached here. Hub browsing/installs
    initiated by that user reuse the JWT until it expires.
    """

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


# ── v2 schema additions (Sprint F + gateway sprint E) ─────────────


class UserDevice(Base):
    """A registered device with per-device notification preferences.

    Replaces the old (``inbox_devices`` + ``inbox_notification_prefs``)
    pair. One row per (user_id, fcm_token); ``prefs`` and
    ``subscribed_topics`` are JSONB so the schema can evolve without
    migration. ``active=False`` is a soft-delete (FCM 410, user logout).
    """

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
    """Catalog of every audit ``action_key`` we emit, with retention.

    Seeded with 18 canonical action_keys at migration time. Operators
    can add custom rows. The retention sweeper reads ``retention_days``
    to know how long to keep matching rows in ``history_log`` and
    ``credential_audit``.
    """

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
