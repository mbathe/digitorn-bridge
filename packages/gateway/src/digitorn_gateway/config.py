"""Settings for the digitorn gateway.

Three layers, in priority order (highest wins):
    env vars (DIGITORN_GATEWAY_*)  >  ~/.digitorn/gateway.env  >  defaults

The env file mirrors the daemon's pattern of keeping per-user state
under ``~/.digitorn/`` (alongside ``config.yaml``, ``master.key``,
``credentials.json`` …). Operators set their secrets there once and
every shell can launch the gateway without re-exporting variables.

Model aliases are NOT in this file - they live in `models.yaml` and
are loaded by `models.load_catalog()`. That keeps the operator able
to add a model without redeploying code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Two env-file lookup paths, in priority order (lower wins per
# pydantic-settings semantics: later entries in the tuple OVERRIDE
# earlier ones for any shared key).
#
#   1. ``/etc/digitorn/digitorn.env`` -- the systemd EnvironmentFile on
#      Hetzner / any production host. Loaded for ad-hoc invocations
#      (deploy.sh preflight, alembic upgrade, manual ``python -m
#      digitorn_gateway``) that DON'T inherit systemd's env injection.
#   2. ``~/.digitorn/gateway.env`` -- per-user dev file. Wins over the
#      system file so a developer can override on their machine without
#      touching the shared one.
#
# OS env vars (DIGITORN_GATEWAY_*) still take priority over BOTH, per
# pydantic-settings's source ordering. Missing files are silently
# ignored, so this is safe on Windows / CI / fresh checkouts.
_SYSTEM_ENV_FILE = Path("/etc/digitorn/digitorn.env")
_USER_ENV_FILE = Path.home() / ".digitorn" / "gateway.env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DIGITORN_GATEWAY_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
        env_file=(_SYSTEM_ENV_FILE, _USER_ENV_FILE),
        env_file_encoding="utf-8",
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
        description="Primary expected `iss` claim on every JWT.",
    )
    auth_accept_issuers: list[str] = Field(
        default_factory=list,
        description=(
            "Additional `iss` values accepted beyond ``auth_issuer``. "
            "Useful when the auth service is reachable via several "
            "URLs (cluster + edge proxy + dev loopback) or when "
            "transitioning between issuer formats (e.g. legacy "
            "'digitorn' label -> canonical 'https://api.digitorn.ai'). "
            "Set as a JSON array via env: "
            "``DIGITORN_GATEWAY_AUTH_ACCEPT_ISSUERS='[\"digitorn\"]'``."
        ),
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

    # Envelope encryption master key for the credentials vault. Wraps
    # the per-row data keys; rotation requires re-wrapping every row.
    # Empty disables the writable-config endpoints (the gateway still
    # serves dispatch traffic, but admin CRUD on credentials is off).
    master_key: str = Field(
        default="",
        description=(
            "Url-safe base64 (or hex / 32-byte raw) master key. Generate "
            "with: python -c \"import os, base64; print("
            "base64.urlsafe_b64encode(os.urandom(32)).decode())\""
        ),
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

    # ── Response caching (opt-in, hot-path-safe) ──────────────────
    # When ``cache_enabled`` is False, the cache code path is bypassed
    # entirely - zero overhead, no Redis lookup, no key computation.
    # When True, callers still need to opt-in PER REQUEST via the
    # ``X-Digitorn-Cache: enabled`` header. The lookup has a hard 5ms
    # timeout so a misbehaving Redis can never slow the hot path.
    cache_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for response caching. False (default) = the "
            "cache module is fully bypassed and adds zero overhead. "
            "True = per-request opt-in via the ``X-Digitorn-Cache: "
            "enabled`` header is honoured. Streaming, temperature>0, "
            "and tool calls are never cached. Enable when you have a "
            "workload with templated/repeating prompts."
        ),
    )
    cache_redis_url: str = Field(
        default="",
        description=(
            "Redis URL used by the response cache "
            "(e.g. ``redis://localhost:6379/3``). Distinct from "
            "``quota_redis_url`` so cache and quota can use different "
            "DBs. Empty = in-process LRU (no cross-worker sharing)."
        ),
    )
    cache_default_ttl_seconds: int = Field(
        default=3600,
        description="TTL applied to every cached response. 1h default.",
        ge=10, le=86400,
    )
    cache_lookup_timeout_ms: int = Field(
        default=5,
        description=(
            "Hard cap on the cache GET. Beyond this we fall through to "
            "a normal dispatch - the cache becomes a free upgrade, "
            "never a slowdown."
        ),
        ge=1, le=200,
    )

    # ── Failover (cross-provider, route-priority-walking) ────────
    # The dispatch loop already walks priority-ordered routes for the
    # same alias on retriable errors. These knobs let an operator turn
    # the loop off (debug / incident lockdown) or cap how many routes
    # are tried, and control automatic load-balancing under saturation.
    failover_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for cross-provider failover. When False, only "
            "the first healthy route is attempted; on failure the user "
            "gets the upstream error directly. Use during incidents to "
            "stop a failure cascade or to debug a single provider."
        ),
    )
    failover_max_attempts: int = Field(
        default=8,
        ge=1, le=16,
        description=(
            "Hard cap on routes the dispatch loop will try for a single "
            "request. Includes the primary attempt. Lowering it speeds "
            "up the worst-case latency at the cost of resilience."
        ),
    )
    failover_max_inflight_per_credential: int = Field(
        default=0,
        ge=0,
        description=(
            "Per-credential inflight cap for load-aware routing. When "
            ">0, a credential whose current inflight count >= cap is "
            "skipped during route resolution and the next route (often "
            "the next tier / next provider) gets the request. Avoids "
            "queueing on a saturated provider when a healthy fallback "
            "is available. 0 = unlimited (legacy behaviour)."
        ),
    )

    # ── Auto-truncation (opt-in safety net) ──────────────────────
    # Two modes layered on the same flag:
    #
    # ``reject`` mode (pre-flight): count tokens once before dispatch.
    # If they exceed the resolved model's max_context (minus the
    # reserved max_tokens for output), return a clean HTTP 413 with a
    # human-readable trim instruction instead of forwarding to the
    # provider and getting back a 400. Catches the "context exceeded"
    # error 200ms-2s earlier and explains how to fix.
    #
    # ``head_drop`` mode (per-route during failover): when the failover
    # loop tries a route whose max_context is smaller than the request,
    # drop the oldest non-system messages in atomic turn-blocks
    # (assistant+tool_calls+tool_responses stay grouped) until the
    # request fits. Only fires on a fallback that wouldn't otherwise
    # accept the request, so the primary path stays at zero overhead.
    #
    # When ``truncate_enabled=False`` both branches are short-circuited
    # by a single ``if`` check; the tokenizer is never invoked.
    truncate_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for auto-truncation. False (default) = both "
            "modes are bypassed entirely (zero overhead, zero tokenizer "
            "call). True = pre-flight reject + per-route head_drop on "
            "fallback are honoured. Enable when you have third-party "
            "clients that don't manage their own context windows."
        ),
    )
    truncate_default_max_output_tokens: int = Field(
        default=1024,
        ge=1, le=131072,
        description=(
            "Reserved output budget when the request omits "
            "``max_tokens``. Used by the truncation guard to compute "
            "``budget = max_context - max_output_tokens``. The actual "
            "request is not modified - this is purely an estimation."
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
            "http://localhost:8081,http://localhost:8082,"
            "http://127.0.0.1:5173,http://127.0.0.1:8080,"
            "http://127.0.0.1:8081,http://127.0.0.1:8082"
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
