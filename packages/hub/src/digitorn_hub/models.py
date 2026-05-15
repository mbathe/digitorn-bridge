from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .settings import get_settings

_EMB_DIM = get_settings().embedding_dim


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    publishers: Mapped[list["Publisher"]] = relationship(back_populates="owner")
    api_tokens: Mapped[list["ApiToken"]] = relationship(back_populates="user")

    __table_args__ = (
        CheckConstraint("role IN ('user','admin')", name="ck_users_role"),
    )


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="api_tokens")


class Publisher(Base):
    __tablename__ = "publishers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    bio: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(String(255))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    owner: Mapped[User] = relationship(back_populates="publishers")
    packages: Mapped[list["Package"]] = relationship(back_populates="publisher")

    __table_args__ = (
        CheckConstraint(
            r"slug ~ '^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$'",
            name="ck_publishers_slug_format",
        ),
    )


class Package(Base):
    __tablename__ = "packages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    publisher_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("publishers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    package_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(40), index=True)
    risk_level: Mapped[str] = mapped_column(String(10), default="medium", nullable=False)
    icon_url: Mapped[str | None] = mapped_column(String(512))
    # When the icon is Hub-served (uploaded via `_maybe_push_icon`),
    # this stores the file extension so the `/icon` route can compose
    # the S3 key `icons/{publisher_slug}/{package_id}.{ext}` without
    # a list call. NULL means: either no icon, or `icon_url` is an
    # absolute publisher-hosted URL.
    icon_storage_ext: Mapped[str | None] = mapped_column(String(8), nullable=True)

    latest_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("package_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    total_downloads: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    avg_rating: Mapped[float | None] = mapped_column(Numeric(3, 2), nullable=True)
    review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    search_vector: Mapped[Any] = mapped_column(TSVECTOR)
    embedding: Mapped[Any] = mapped_column(Vector(_EMB_DIM), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    publisher: Mapped[Publisher] = relationship(back_populates="packages")
    versions: Mapped[list["PackageVersion"]] = relationship(
        back_populates="package",
        foreign_keys="PackageVersion.package_id",
        cascade="all, delete-orphan",
    )
    tags: Mapped[list["PackageTag"]] = relationship(
        back_populates="package", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("publisher_id", "package_id", name="uq_packages_publisher_pkgid"),
        CheckConstraint(
            "risk_level IN ('low','medium','high')", name="ck_packages_risk_level"
        ),
        Index("ix_packages_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_packages_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class PackageVersion(Base):
    __tablename__ = "package_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    package_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    archive_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    archive_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    yanked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    yanked_reason: Mapped[str | None] = mapped_column(Text)
    yanked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    downloads: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    released_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    package: Mapped[Package] = relationship(
        back_populates="versions", foreign_keys=[package_id]
    )

    __table_args__ = (
        UniqueConstraint("package_id", "version", name="uq_package_versions_pkg_ver"),
    )


class PackageTag(Base):
    __tablename__ = "package_tags"

    package_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("packages.id", ondelete="CASCADE"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(String(40), primary_key=True)

    package: Mapped[Package] = relationship(back_populates="tags")

    __table_args__ = (
        Index("ix_package_tags_tag", "tag"),
    )


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    package_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    hidden_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("package_id", "user_id", name="uq_reviews_pkg_user"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_reviews_rating_range"),
        CheckConstraint(
            "body IS NULL OR length(body) <= 4000",
            name="ck_reviews_body_max_len",
        ),
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    package_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reporter_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "reason IN ('malware','spam','abuse','copyright','broken','other')",
            name="ck_reports_reason",
        ),
        CheckConstraint(
            "status IN ('open','reviewing','resolved','rejected')",
            name="ck_reports_status",
        ),
    )


class DownloadEvent(Base):
    __tablename__ = "download_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    package_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("package_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    daemon_fingerprint: Mapped[str | None] = mapped_column(String(64))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


# TrustedDaemon + DaemonBridgeNonce were retired when the Hub started
# accepting central RS256 JWTs natively (see digitorn_hub.auth.central).
# The corresponding tables (trusted_daemons, daemon_bridge_nonces) are
# dropped by alembic migration 0005_drop_daemon_bridge.


# ── MCP registry (mirror of registry.modelcontextprotocol.io) ────────


class McpRegistryEntry(Base):
    """One MCP server cached from ``registry.modelcontextprotocol.io``.

    The Hub periodically pulls the upstream registry (every 24h via
    ``mcp_registry_ingester``), upserts each row, and rebuilds the FTS
    ``search_vector`` + semantic ``embedding`` columns. Daemons proxy
    their ``/api/mcp/registry/*`` calls here so the hot path never
    talks to the upstream registry directly.

    Search runs as the same hybrid stack used for packages: FTS via
    ``websearch_to_tsquery`` ranked by ``ts_rank_cd``, semantic kNN
    via pgvector cosine, then combined with reciprocal rank fusion.
    """

    __tablename__ = "mcp_registry_entries"

    # Short slug (last segment of the upstream ``name``, e.g. ``github``
    # for ``smithery-ai/github``). Stable across registry versions.
    server_id: Mapped[str] = mapped_column(String(120), primary_key=True)

    # Fully-qualified upstream name (``org/server``). Source of truth
    # for credit / linking back to the publisher in the registry.
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)

    # Runtime / package metadata flattened from upstream ``packages[0]``
    # and ``remotes[0]``. ``runtime == 'none'`` means remote-only
    # (no npm/pip package to install — uses an HTTP endpoint).
    runtime: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    package: Mapped[str | None] = mapped_column(String(255))
    transport: Mapped[str] = mapped_column(String(20), default="stdio", nullable=False)

    has_oauth: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    required_env_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # ``env_var_names`` is the flat list of env var names the server
    # reads (e.g. ``["GITHUB_PERSONAL_ACCESS_TOKEN"]``). Used for the
    # search vector + the install dialog hint.
    env_var_names: Mapped[list[str]] = mapped_column(
        ARRAY(String(120)), default=list, nullable=False
    )

    version: Mapped[str | None] = mapped_column(String(40))
    repository_url: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)

    # Full upstream JSON entry. Lets us reconstruct the install config
    # later (transport-specific args / headers) without re-querying the
    # registry, and gives future code the option to surface fields we
    # haven't flattened (categories, tags, ...).
    raw_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Hybrid-search columns (mirror of ``packages``).
    search_vector: Mapped[Any] = mapped_column(TSVECTOR)
    embedding: Mapped[Any] = mapped_column(Vector(_EMB_DIM), nullable=True)

    # When the ingester first saw / last saw this server. Difference
    # between ``last_seen_at`` and "now" tells us if the entry vanished
    # upstream (we keep stale rows for a while so the dashboard doesn't
    # flip empty during a registry hiccup).
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "runtime IN ('npm','pip','none','custom')",
            name="ck_mcp_registry_runtime",
        ),
        CheckConstraint(
            "transport IN ('stdio','sse','streamable_http','http','ws')",
            name="ck_mcp_registry_transport",
        ),
        CheckConstraint(
            "status IN ('active','deprecated','retired')",
            name="ck_mcp_registry_status",
        ),
        Index(
            "ix_mcp_registry_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
        Index(
            "ix_mcp_registry_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_mcp_registry_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_mcp_registry_runtime", "runtime"),
        Index("ix_mcp_registry_status", "status"),
        Index("ix_mcp_registry_last_seen_at", "last_seen_at"),
    )
