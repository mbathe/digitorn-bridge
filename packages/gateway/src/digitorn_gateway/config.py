"""Settings for the digitorn gateway.

Two layers, in priority order (highest wins):
    env vars (DIGITORN_GATEWAY_*)  >  defaults

Model aliases are NOT in this file - they live in `models.yaml` and
are loaded by `models.load_catalog()`. That keeps the operator able
to add a model without redeploying code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DIGITORN_GATEWAY_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8002, ge=1024, le=65535)
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    # Auth: JWKS endpoint of the central digitorn auth service.
    # The gateway pulls this at boot, caches the public keys, and
    # verifies every incoming JWT offline against the cached set.
    auth_jwks_url: str = Field(
        default="https://auth.digitorn.ai/.well-known/jwks.json",
        description="JWKS endpoint exposed by digitorn-auth.",
    )
    auth_issuer: str = Field(
        default="https://auth.digitorn.ai",
        description="Expected `iss` claim on every JWT.",
    )
    auth_jwks_refresh_seconds: int = Field(
        default=900,
        description="How often the cached JWKS is re-fetched. 15 min by default.",
    )

    # Model catalogue. YAML file declaring `digitorn-pro` -> Anthropic
    # claude-sonnet-..., etc. See `models.yaml.example` for the shape.
    models_config_path: Path = Field(
        default=Path("config/models.yaml"),
        description="Path to the model alias catalogue YAML.",
    )

    # Database. The gateway connects DIRECTLY to the production
    # Postgres shared with digitorn-auth. The `users` table is the
    # single source of truth for identities - the gateway never owns
    # one of its own. There is no SQLite fallback: a missing
    # DATABASE_URL is a hard failure at boot.
    database_url: str = Field(
        default="",
        description=(
            "Async SQLAlchemy URL for the shared Postgres. Same DB as "
            "digitorn-auth. No SQLite fallback - this MUST be set."
        ),
    )
    database_echo: bool = Field(
        default=False,
        description="Echo SQL statements to the log (dev only).",
    )

    # Quota subsystem behaviour. The defaults are tuned for the
    # never-block-the-request guarantee.
    quota_enabled: bool = Field(
        default=True,
        description=(
            "Master switch. When False, the pre-call check is a no-op "
            "and post-call records are dropped. Use for dev / load tests."
        ),
    )
    quota_flush_interval_seconds: int = Field(
        default=10,
        description=(
            "How often the in-memory counters are flushed to Postgres. "
            "Lower = less data loss on restart, higher = fewer writes."
        ),
    )
    quota_plan_cache_ttl_seconds: int = Field(
        default=300,
        description=(
            "How long a user's plan_id is cached in memory before it is "
            "refreshed from Postgres. Long enough to absorb plan changes "
            "without DB pressure on the hot path."
        ),
    )
    quota_plans_seed_path: Path = Field(
        default=Path("config/plans.yaml"),
        description=(
            "YAML file with the default plans. Seeded into Postgres at "
            "boot if no plans exist yet. After that, the source of truth "
            "is the DB; edits via /admin/quota/plans win."
        ),
    )
    quota_default_plan_name: str = Field(
        default="free",
        description=(
            "Plan assigned to a user when their `plan_id` column is NULL. "
            "Must exist in the seeded plans."
        ),
    )
    quota_redis_url: str = Field(
        default="",
        description=(
            "Redis URL for cross-worker quota coordination "
            "(e.g. ``redis://localhost:6379/2``). When set, the engine "
            "atomically INCRs cluster-wide counters in Redis and "
            "broadcasts sticky blocks via Pub/Sub. The hot-path "
            "``is_blocked()`` check stays in-memory regardless. "
            "Empty = single-process mode (legacy)."
        ),
    )

    # Request budget. Anything above this is rejected with 413 before
    # even hitting the provider - protects upstream providers from
    # rogue clients sending 10MB messages arrays.
    max_request_bytes: int = Field(
        default=10 * 1024 * 1024,
        description="Maximum body size accepted on /v1/chat/completions.",
    )

    # CORS - permit the admin dashboard to call the gateway from a
    # different origin. Comma-separated list. In production set this
    # to your dashboard's URL only; in dev keep localhost variants.
    cors_allow_origins: str = Field(
        default=(
            "http://localhost:5173,http://localhost:3000,http://localhost:8080,"
            "http://localhost:8081,http://127.0.0.1:5173,http://127.0.0.1:8080,"
            "http://127.0.0.1:8081"
        ),
        description="CORS allow_origins as a comma-separated list.",
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def override_settings(s: Settings) -> None:
    global _settings
    _settings = s
