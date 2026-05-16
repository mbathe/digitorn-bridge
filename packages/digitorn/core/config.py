"""Digitorn - Daemon configuration.

Configuration is loaded from (in order of increasing priority):
    1. Built-in defaults (this file)
    2. System config: /etc/digitorn/config.yaml
    3. User config:   ~/.digitorn/config.yaml
    4. Environment variables prefixed with DIGITORN_

Nested env vars use double underscore as separator:
    DIGITORN_SERVER__PORT=8000
    DIGITORN_DATABASE__URL=postgresql+asyncpg://...
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Re-export ``WorkersConfig`` so the ``workers:`` block on Settings
# is a real nested model. The workers subsystem owns the schema --
# core/config.py is the integration point. Import is cheap (only
# pydantic) and creates no cycle (workers/config.py imports nothing
# from digitorn.core).
from digitorn.workers.config import WorkersConfig




class ServerConfig(BaseModel):
    """FastAPI / Uvicorn server settings."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1024, le=65535)
    workers: int = Field(default=1, ge=1, le=16)
    reload: bool = False
    rate_limit_rpm: int = Field(
        default=100_000,
        ge=1,
        le=100_000,
        description=(
            "Default requests per minute per app (rate limiting). "
            "Effectively disabled - set to the upper bound. Specific "
            "buckets (auth, admin, deploy) still have their own tighter "
            "caps applied in server.py."
        ),
    )
    kv_backend: str | None = Field(
        default=None,
        description=(
            "Key-value backend URL for sessions, rate limits, and caches. "
            "Use 'redis://host:6379/0' for multi-host production. "
            "Default (None) uses DiskCache (SQLite-backed, single-host)."
        ),
    )
    auth_enabled: bool = Field(
        default=True,
        description="Enable JWT/API-key authentication on all API endpoints.",
    )
    expose_docs: bool = Field(
        default=False,
        description=(
            "Expose Swagger UI (/docs), ReDoc (/redoc), and the raw "
            "OpenAPI schema (/openapi.json). Automatically true when "
            "``auth_enabled`` is false (dev mode). Leave off in prod - the "
            "full API surface is an attacker's best friend."
        ),
    )
    turn_workers: int = Field(
        default=32,
        ge=1,
        le=512,
        description="Thread pool size for agent turns. Each turn uses one thread.",
    )
    io_workers: int = Field(
        default=64,
        ge=1,
        le=1024,
        description="Thread pool size for blocking I/O (KV backend, filesystem).",
    )
    sandbox: bool = Field(
        default=True,
        description=(
            "Enable OS-level sandbox for deployed apps. "
            "Apps with capabilities: are forked into isolated workers "
            "with Landlock/seccomp (Linux), Seatbelt (macOS), or Job Objects (Windows)."
        ),
    )
    node_auto_install: bool = Field(
        default=True,
        description=(
            "Auto-download Node.js LTS at daemon boot if no suitable Node "
            "is found in PATH or a supported version manager (nvm/fnm/volta). "
            "Disable in air-gapped / CI environments - Node-dependent features "
            "(MCP Node servers, app preview) will then require a pre-installed Node."
        ),
    )
    cors_origins: list[str] = Field(
        default=[
            "https://app.digitorn.ai",
            "https://api.digitorn.ai",
            "http://localhost", "http://127.0.0.1",
            "http://localhost:4000", "http://localhost:5173", "http://localhost:3000",
            "http://127.0.0.1:4000", "http://127.0.0.1:5173", "http://127.0.0.1:3000",
            "https://localhost:3000", "https://localhost:4000",
        ],
        description=(
            "Allowed CORS origins. "
            "Add your frontend URL for production deployments."
        ),
    )

    @field_validator("cors_origins")
    @classmethod
    def _reject_wildcard_cors(cls, v: list[str]) -> list[str]:
        if "*" in v:
            raise ValueError(
                "Wildcard '*' CORS origin is not allowed - "
                "specify explicit origins instead."
            )
        return v


class OAuthProviderConfig(BaseModel):
    """Credentials for a single OAuth provider (Google, Microsoft, etc.)."""

    client_id: str = Field(default="", description="OAuth client ID")
    client_secret: str = Field(default="", description="OAuth client secret")

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)


class OAuthConfig(BaseModel):
    """OAuth/OIDC providers config. Each sub-section is enabled when both
    `client_id` and `client_secret` are set (typically via env vars):

        DIGITORN_OAUTH__GOOGLE__CLIENT_ID=...
        DIGITORN_OAUTH__GOOGLE__CLIENT_SECRET=...
        DIGITORN_OAUTH__MICROSOFT__CLIENT_ID=...
        DIGITORN_OAUTH__MICROSOFT__CLIENT_SECRET=...

    The redirect URI is derived from `public_base_url` at runtime, e.g.
    `https://api.digitorn.ai/auth/oauth/google/callback`.
    """

    public_base_url: str = Field(
        default="http://localhost:8000",
        description=(
            "Public base URL of this daemon, used to build OAuth redirect "
            "URIs. Must match the URI registered in Google/Microsoft "
            "consoles. In prod typically https://api.digitorn.ai."
        ),
    )
    web_origin: str = Field(
        default="http://localhost:3000",
        description=(
            "Frontend origin where the user is bounced back after OAuth "
            "(carries the JWT in the URL fragment). Set to "
            "https://app.digitorn.ai in prod."
        ),
    )
    google: OAuthProviderConfig = Field(default_factory=OAuthProviderConfig)
    microsoft: OAuthProviderConfig = Field(default_factory=OAuthProviderConfig)


class DatabaseConfig(BaseModel):
    """SQLAlchemy database connection.

    Supports any SQLAlchemy-compatible URL:
        - SQLite:      sqlite+aiosqlite:///~/.digitorn/digitorn.db
        - PostgreSQL:  postgresql+asyncpg://user:pass@localhost/digitorn
        - MySQL:       mysql+aiomysql://user:pass@localhost/digitorn
    """

    url: str = Field(
        default="sqlite+aiosqlite:///digitorn.db",
        description="SQLAlchemy async database URL.",
    )
    echo: bool = Field(
        default=False,
        description="Echo SQL statements to stdout (debug only).",
    )
    pool_size: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Connection pool size (ignored for SQLite).",
    )


class ModulesConfig(BaseModel):
    """Module discovery and loading."""

    paths: list[str] = Field(
        default_factory=lambda: ["modules"],
        description=(
            "Directories to scan for modules (relative to packages/digitorn/ "
            "or absolute paths)."
        ),
    )
    enabled: list[str] = Field(
        default_factory=list,
        description="Module IDs to load. Empty = load all discovered modules.",
    )
    disabled: list[str] = Field(
        default_factory=list,
        description="Module IDs to exclude (overrides 'enabled').",
    )
    load_all: bool = Field(
        default=True,
        description=(
            "If True, load all discovered modules with enabled=true in their TOML. "
            "If False, only load modules explicitly listed in 'enabled'."
        ),
    )


class AppConfig(BaseModel):
    """Application YAML configuration."""

    yaml_path: str | None = Field(
        default=None,
        description=(
            "Path to an application YAML file to compile and bootstrap at startup. "
            "Supports absolute paths or relative to the working directory."
        ),
    )
    stop_on_error: bool = Field(
        default=False,
        description="Stop bootstrap if any setup step fails.",
    )
    hot_reload: bool = Field(
        default=False,
        description=(
            "Dev mode: watch each deployed app's prompts/, "
            "skills/, and assets/ directories and auto-redeploy "
            "on change. Leave false in production."
        ),
    )


class RuntimeConfig(BaseModel):
    """Agent runtime tuning knobs.

    These control loop detection, tool timeouts, context compaction,
    and other runtime behaviors that were previously hardcoded.
    """

    max_consecutive_failures: int = Field(
        default=8, ge=1, le=30,
        description="Consecutive tool failures before the loop detector warns the LLM.",
    )
    max_repeat_window: int = Field(
        default=20, ge=2, le=100,
        description="Sliding window size for duplicate tool-call detection.",
    )
    max_repeats: int = Field(
        default=8, ge=1, le=30,
        description="Max identical calls within the repeat window before warning.",
    )
    max_consecutive_same_tool: int = Field(
        default=30, ge=1, le=100,
        description="Max consecutive calls to the same tool before warning.",
    )
    tool_timeout: float = Field(
        default=3600.0, ge=1.0, le=7200.0,
        description="Default per-tool execution timeout in seconds.",
    )
    context_pressure_threshold: float = Field(
        default=0.75, ge=0.1, le=0.99,
        description="Token pressure ratio that triggers compaction (0.0-1.0).",
    )
    specialist_context_window: int = Field(
        default=50_000, ge=4_000, le=2_000_000,
        description="Default context window size for specialist agents.",
    )
    watch_poll_interval: int = Field(
        default=5, ge=1, le=300,
        description="File watch trigger polling interval in seconds.",
    )
    tracking: "RunTrackingConfig" = Field(default_factory=lambda: RunTrackingConfig())

    # ── Gateway routing for outbound LLM calls ─────────────────────
    # When a session has no per-app / per-user credential configured
    # for the brain's provider, the runtime hot-swaps the brain config
    # to call this URL with the user's JWT instead. The gateway
    # authenticates the call, checks quota, forwards to the real
    # provider with Digitorn's shared API keys. Default uses the
    # loopback address - daemon and gateway tend to cohabit on the
    # same VPS, so loopback skips DNS, TLS, and the public Caddy hop.
    gateway_base_url: str = Field(
        default="http://127.0.0.1:8002/v1",
        description=(
            "Base URL the daemon uses to reach the digitorn gateway. "
            "Default = local loopback (same machine deployment). "
            "Set to ``https://gateway.digitorn.ai/v1`` for split deployments."
        ),
    )
    gateway_enabled: bool = Field(
        default=True,
        description=(
            "If False, the runtime never hot-swaps brains to route "
            "via the gateway. Apps without a user-configured credential "
            "fail with a clear error message - useful for local-only "
            "deployments where users bring their own provider keys."
        ),
    )


class RunTrackingConfig(BaseModel):
    """Pluggable persistence for agent_runs / agent_run_events.

    The runtime hot path enqueues events to a single async worker that
    drains them into the configured backend. The runtime never blocks
    on persistence; a slow or unavailable backend translates to events
    queueing up (capped) then dropped with a warning, NEVER to a
    slowdown of the user-facing turn.

    Modes:

      * ``postgres`` (cloud, default) - writes to the v2 schema in the
        same Neon Postgres the daemon uses for everything else. Powers
        the live dashboard.

      * ``jsonfile`` (local) - one append-only JSONL per run under
        ``~/.digitorn/runs/<YYYY>/<MM>/<run_id>.jsonl``. No external
        service required; fits the runtime-only deployment story.

      * ``null`` (off) - drop every event silently. For benchmarks
        and trust-no-one sandbox runs.
    """

    backend: str = Field(
        default="jsonfile",
        description=(
            "Backend key (``postgres`` | ``sqlite`` | ``jsonfile`` | "
            "``kv`` | ``null``). Operators can register custom backends "
            "via ``digitorn.core.runtime.run_tracker.backends.BACKEND_REGISTRY``. "
            "Default is ``jsonfile``: append-only JSONL per session, "
            "no external service required, scales to 1M+ sessions, and "
            "the data stays available for offline analytics by tailing "
            "the files. Switch to ``postgres`` for a multi-instance "
            "cloud deployment that needs a single SQL surface for the "
            "admin dashboard."
        ),
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Backend-specific options. ``jsonfile`` and ``sqlite`` "
            "accept ``{path: str}`` (defaults to ~/.digitorn/runs/). "
            "``postgres``, ``kv`` and ``null`` ignore this."
        ),
    )


class AuthConfig(BaseModel):
    """Authentication and authorization settings."""

    access_token_ttl: int = Field(
        default=0, ge=0,
        description=(
            "Access token lifetime in seconds. ``0`` means never expires "
            "(the JWT is issued without an ``exp`` claim). Default: 0 - "
            "tokens are long-lived for local-dev ergonomics. Set to a "
            "positive int (e.g. 900 for 15min) in production."
        ),
    )
    refresh_token_ttl: int = Field(
        default=0, ge=0,
        description=(
            "Refresh token lifetime in seconds. ``0`` means never expires. "
            "Default: 0 - see access_token_ttl for rationale."
        ),
    )
    max_login_failures: int = Field(
        default=5, ge=1, le=100,
        description="Lock account after N failed login attempts.",
    )
    lockout_window: int = Field(
        default=900, ge=60, le=86400,
        description="Lockout window in seconds (default: 15 min).",
    )
    approval_timeout: float = Field(
        default=3600.0, ge=10.0, le=7200.0,
        description="Time to wait for user approval before auto-deny (seconds).",
    )

    # ── Central auth integration ─────────────────────────────────────
    # The daemon TRUSTS tokens issued by the central digitorn-auth
    # service: it caches the central's JWKS via
    # ``digitorn_auth.client.RemoteAuthClient`` and verifies signatures
    # with the central's RSA public key. Combined with
    # ``LocalDeviceAuth`` (paired via ``digitorn install-local``) the
    # daemon can authenticate the user fully offline.
    mode: str = Field(
        default="remote",
        description=(
            "Auth mode. Only ``'remote'`` is supported. The daemon "
            "validates tokens issued by the central digitorn-auth "
            "service at ``service_url``; it never signs its own JWTs."
        ),
    )
    service_url: str = Field(
        default="https://auth.digitorn.ai",
        description=(
            "Base URL of the central digitorn-auth service. Defaults "
            "to Digitorn's hosted auth so a fresh install boots "
            "without manual config; override here or via env "
            "``DIGITORN_AUTH__SERVICE_URL`` to point at a self-hosted "
            "instance (e.g. ``http://127.0.0.1:8001`` in dev)."
        ),
    )
    accept_issuers: list[str] = Field(
        default_factory=list,
        description=(
            "Extra `iss` claim values the daemon accepts in tokens "
            "(beyond the configured `service_url`). Useful when the "
            "auth service is reached via multiple URLs (cluster + "
            "edge proxy + dev loopback)."
        ),
    )
    enable_local_device: bool = Field(
        default=False,
        description=(
            "Load LocalDeviceAuth secrets at daemon start and run the "
            "device revalidator background task. Requires the daemon "
            "to have been paired via `digitorn install-local`. Only "
            "meaningful in mode='remote'."
        ),
    )


class SessionConfig(BaseModel):
    """Session management settings."""

    idle_ttl: int = Field(
        default=1800, ge=0, le=604800,
        description="Session expires after N seconds of inactivity (default: 30 min). 0 = never expire.",
    )
    absolute_ttl: int = Field(
        default=86400, ge=0, le=2592000,
        description="Session expires after N seconds regardless of activity (default: 24h). 0 = never expire.",
    )
    max_events_per_turn: int = Field(
        default=500, ge=50, le=5000,
        description="Safety cap on events emitted per turn to prevent memory bloat.",
    )
    max_sessions_per_app: int = Field(
        default=100, ge=1, le=10000,
        description="Max active sessions per deployed app.",
    )
    lock_timeout: float = Field(
        default=600.0, ge=5.0, le=3600.0,
        description=(
            "How long (seconds) a new turn waits for the session lock "
            "before giving up with `session_busy`. Default 600s (10 min) "
            "gives room for long agent runs (big refactors, multi-step "
            "research) while still bounding stuck turns. When the queue "
            "feature is enabled, incoming messages wait in the per-session "
            "queue instead of erroring out until this timeout expires."
        ),
    )
    queue: "SessionQueueConfig" = Field(
        default_factory=lambda: SessionQueueConfig(),
        description=(
            "Per-session message queue settings. When enabled, messages "
            "sent while a turn is running are enqueued in a persistent "
            "FIFO queue instead of erroring out. Survives daemon restart."
        ),
    )
    approval_timeout_s: float = Field(
        default=300.0, ge=5.0, le=86400.0,
        description=(
            "How long an agent waits for a user to resolve an approval "
            "request before auto-denying with ``approval_timeout``. "
            "Apps with a compiled security_profile.approval_timeout "
            "override this default. Keep low (30–120 s) for headless / "
            "CI contexts, higher (5–15 min) for interactive UI where a "
            "user is expected to click within a reasonable window."
        ),
    )


class SessionQueueConfig(BaseModel):
    """Per-session message queue settings."""

    enabled: bool = Field(
        default=True,
        description=(
            "Feature flag. When False, POST /messages falls back to the "
            "legacy lock-based flow (session_busy on contention)."
        ),
    )
    max_depth: int = Field(
        default=20, ge=1, le=1000,
        description=(
            "Max queued messages per session. Over the cap, POST /messages "
            "returns 429 + a `queue_full` event."
        ),
    )
    ttl_seconds: int = Field(
        default=1200, ge=60, le=86400,
        description=(
            "Queued messages auto-expire after this many seconds. Default "
            "20 min. Expired messages are skipped at dequeue time and "
            "marked failed with error_code=queue_ttl_expired."
        ),
    )
    auto_merge: bool = Field(
        default=False,
        description=(
            "When True, consecutive user messages enqueued within "
            "`auto_merge_window_s` are concatenated into a single turn. "
            "Saves LLM calls when the user sends rapid follow-ups."
        ),
    )
    auto_merge_window_s: float = Field(
        default=2.0, ge=0.5, le=30.0,
        description="Window in seconds for auto_merge.",
    )
    default_mode: Literal["wait", "async"] = Field(
        default="async",
        description=(
            "How POST /messages behaves: "
            "'wait' = legacy compat, block until the turn finishes and "
            "return the result; "
            "'async' = enqueue + return 202 with correlation_id, "
            "client tracks via SSE. Async is the recommended mode."
        ),
    )
    backend: Literal["redis", "memory"] = Field(
        default="redis",
        description=(
            "Storage backend for the message queue. "
            "'redis' (default, prod) uses Redis lists+hashes via Lua "
            "scripts - sub-ms, atomic mark_done+drain (closes the "
            "running/queued race). "
            "'memory' is process-local, lost on restart - for dev "
            "without a local Redis and for tests."
        ),
    )
    redis_url: str | None = Field(
        default=None,
        description=(
            "Redis URL for backend='redis'. When None, falls back to "
            "ServerConfig.kv_backend if it starts with redis://. "
            "If neither resolves to a Redis URL the daemon refuses to "
            "boot with backend='redis' (operators must fix the URL or "
            "set backend='memory' for dev)."
        ),
    )


class AgentSpawnConfig(BaseModel):
    """Sub-agent spawning settings."""

    max_workers: int = Field(
        default=20, ge=1, le=500,
        description=(
            "Max parallel sub-agents per session. The hard upper bound "
            "is 500 (was 50). Real concurrency at the upper end depends "
            "on RAM (each agent ~50-100 MB context), DB pool, and the "
            "LLM provider's rate-limits. The default 20 is a sensible "
            "starting point - scale up after measuring."
        ),
    )
    max_workers_global: int = Field(
        default=200, ge=1, le=2000,
        description=(
            "Hard ceiling on total concurrent sub-agents across all "
            "sessions. Prevents one runaway session from monopolizing "
            "the daemon. Tune up for high-fan-out workloads. The "
            "computed default is ``max_workers * 4`` clamped to "
            "[100, 2000]; this field overrides that auto-derivation."
        ),
    )
    max_turns: int = Field(
        default=100, ge=10, le=10000,
        description="Default max turns per sub-agent.",
    )
    timeout: float = Field(
        default=3600.0, ge=30.0, le=7200.0,
        description="Default sub-agent timeout in seconds.",
    )
    cleanup_age: float = Field(
        default=300.0, ge=30.0, le=86400.0,
        description="Remove completed sub-agents after N seconds.",
    )
    cleanup_interval: float = Field(
        default=30.0, ge=5.0, le=600.0,
        description=(
            "How often the background cleanup task runs. Keeps the "
            "spawn hot path O(1) - no per-spawn iterate over every "
            "agent. Lower = more frequent reclamation, higher = less "
            "background CPU."
        ),
    )
    max_seed_total_chars: int = Field(
        default=4000, ge=500, le=20000,
        description=(
            "Hard cap on the inherited-context block injected into a "
            "sub-agent's system prompt. Per-section caps (5 sub-goals, "
            "8 todos, 7 facts, 300 char/item) are enforced first; this "
            "is the belt-and-braces ceiling. Tune up only if your "
            "coordinator legitimately accumulates large state worth "
            "propagating - it directly inflates child token bills."
        ),
    )
    max_cached_sessions: int = Field(
        default=100, ge=10, le=10000,
        description=(
            "LRU bound on the per-session module/ContextBuilder cache. "
            "``cleanup_session`` already drops the entry on normal "
            "session end, but daemon crashes mid-session leave orphans. "
            "Past this threshold the periodic cleanup task evicts the "
            "least-recently-used entries (calling ``on_stop`` on owned "
            "modules). Each entry holds a full module set + index, "
            "easily 50-100 MB at scale - keep this tight."
        ),
    )


class MCPConfig(BaseModel):
    """MCP (Model Context Protocol) server management."""

    health_interval: int = Field(
        default=60, ge=10, le=600,
        description="Health check interval for MCP servers (seconds).",
    )
    process_timeout: float = Field(
        default=60.0, ge=5.0, le=600.0,
        description="MCP subprocess execution timeout (seconds).",
    )
    max_reconnect_attempts: int = Field(
        default=4, ge=0, le=20,
        description="Max reconnect attempts for failed MCP servers.",
    )
    tool_call_timeout: float = Field(
        default=3600.0, ge=5.0, le=7200.0,
        description="Timeout for individual MCP tool calls (seconds).",
    )


class SandboxConfig(BaseModel):
    """Sandbox and process isolation settings."""

    pool_size: int = Field(
        default=2, ge=0, le=20,
        description=(
            "Number of warm sandbox workers pre-spawned per app at boot. "
            "Set to 0 to disable pre-warming entirely - workers will spawn "
            "lazily on first session use (+~3-5s first turn). Recommended "
            "for dev / Windows machines where parallel cold imports of "
            "fastembed / ONNX saturate the disk and trigger pool_warm "
            "timeouts."
        ),
    )
    idle_timeout: float = Field(
        default=60.0, ge=10.0, le=600.0,
        description="Idle sandbox worker cleanup timeout (seconds).",
    )
    max_processes: int = Field(
        default=10, ge=1, le=100,
        description="Max processes per sandbox.",
    )
    drain_timeout: float = Field(
        default=30.0, ge=5.0, le=120.0,
        description="Shutdown drain timeout - wait for active requests (seconds).",
    )


class WebSocketConfig(BaseModel):
    """Socket.IO / WebSocket rate limiting."""

    rate_limit_window: float = Field(
        default=10.0, ge=1.0, le=60.0,
        description="Rate limit window in seconds.",
    )
    rate_limit_max_connections: int = Field(
        default=5, ge=1, le=100,
        description="Max WebSocket connections per IP within the window.",
    )


class ImageConfig(BaseModel):
    """Image handling settings for multimodal support."""

    max_per_message: int = Field(
        default=10, ge=0, le=100,
        description="Max images per message (0 = unlimited).",
    )
    max_size_bytes: int = Field(
        default=10_485_760, ge=0,
        description="Max size per image in bytes (default: 10MB).",
    )
    max_per_session: int = Field(
        default=100, ge=0,
        description="Max total images per session (0 = unlimited).",
    )
    storage_dir: str = Field(
        default="",
        description="Image storage directory. Empty = ~/.digitorn/images/",
    )
    low_res_size: int = Field(
        default=512, ge=64, le=2048,
        description="Resize dimension for aged images (px).",
    )
    aging_full_turns: int = Field(
        default=1, ge=0, le=10,
        description="Number of turns an image stays at full resolution.",
    )
    aging_low_turns: int = Field(
        default=2, ge=0, le=10,
        description="Number of turns an image stays at low resolution before becoming text.",
    )
    cleanup_after_days: int = Field(
        default=7, ge=1, le=365,
        description="Delete stored images after N days.",
    )


class DiscoveryConfig(BaseModel):
    """Tool discovery and semantic search settings (discovery mode)."""

    embedding_model: str = Field(
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        description="FastEmbed model ID for tool semantic search.",
    )
    embedding_dim: int = Field(
        default=384, ge=64, le=4096,
        description="Embedding vector dimension (must match the model).",
    )
    skip_embeddings: bool = Field(
        default=False,
        description="Skip semantic index (saves ~900MB RAM, keyword search only).",
    )
    search_top_k: int = Field(
        default=5, ge=1, le=50,
        description="Default number of results for tool search.",
    )
    search_min_score: float = Field(
        default=0.2, ge=0.0, le=1.0,
        description="Minimum similarity score for semantic search results.",
    )
    models_cache_dir: str = Field(
        default="",
        description="Directory to cache embedding models. Empty = ~/.local/share/digitorn/models/",
    )


class DefaultModelConfig(BaseModel):
    """Default LLM model for built-in apps (digitorn-chat).

    These values are injected into built-in app YAMLs at deploy time,
    so nothing is hardcoded. Override via config.yaml or env vars.
    """

    provider: str = Field(
        default="anthropic",
        description="LLM provider: 'anthropic', 'deepseek', 'openai', 'ollama', etc.",
    )
    model: str = Field(
        default="claude-sonnet-4-20250514",
        description="Model name/ID.",
    )
    backend: str = Field(
        default="",
        description="Backend override: 'openai_compat', etc. Empty = auto-detect from provider.",
    )
    api_key: str = Field(
        default="claude-code",
        description=(
            "API key. Special values: 'claude-code' (use OAuth token), "
            "'env:VAR_NAME' (read from env var). Or a literal key."
        ),
    )
    base_url: str = Field(
        default="",
        description="Base URL override for the API (e.g. 'https://api.deepseek.com/v1').",
    )
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=256, le=65536)
    context_window: int = Field(
        default=200000, ge=4000, le=2000000,
        description="Context window size for the model.",
    )


class LoggingConfig(BaseModel):
    """Logging settings."""

    level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    format: Literal["json", "console"] = "console"


class TranscribeConfig(BaseModel):
    """Voice transcription (`POST /api/transcribe`) settings.

    Three providers supported, picked by ``provider``:
      * ``gateway`` (default) - forward to ``runtime.gateway_base_url``
        so the gateway handles routing, credentials, quota, usage events,
        cost tracking and failover. This is the canonical path: every
        outbound AI call goes through the gateway, transcription
        included.
      * ``openai`` - call OpenAI's ``whisper-1`` endpoint directly. Needs
        the ``OPENAI_API_KEY`` env var or a credential under
        provider='openai'. Only useful when ``runtime.gateway_enabled``
        is false (air-gapped / local-only deploys).
      * ``local`` - run faster-whisper in-process. No network, no cost,
        but needs ``faster-whisper`` + ``av`` installed in the daemon's
        venv and a CUDA-capable GPU for reasonable latency on anything
        bigger than ``tiny``. Pick this only when you want the audio to
        never leave the machine.
    """

    enabled: bool = Field(
        default=True,
        description="Expose POST /api/transcribe. Disable to always return 404.",
    )
    provider: Literal["local", "openai", "gateway"] = "gateway"
    gateway_model: str = Field(
        default="whisper-1",
        description=(
            "Alias to send when ``provider=gateway``. The gateway resolves "
            "this to a real upstream model + credential via its catalogue + "
            "routes tables. Suggested aliases: 'whisper-1' (OpenAI), "
            "'whisper-large-v3' (Groq, free tier), 'whisper-large-v3-turbo'."
        ),
    )
    model: str = Field(
        default="base",
        description=(
            "faster-whisper model size: tiny | base | small | medium | "
            "large-v3. Small = better quality but slower. Ignored when "
            "provider=openai (always whisper-1)."
        ),
    )
    device: Literal["cpu", "cuda", "auto"] = "auto"
    compute_type: str = Field(
        default="int8",
        description=(
            "faster-whisper compute_type: int8 (CPU default), int8_float16 "
            "(CUDA), float16, float32. Only applies to provider=local."
        ),
    )
    max_audio_bytes: int = Field(
        default=25 * 1024 * 1024,
        description="Hard limit on uploaded audio size (default 25 MB).",
    )
    min_audio_bytes: int = Field(
        default=500,
        description="Below this, the audio is rejected as empty/truncated.",
    )
    timeout_seconds: float = Field(
        default=120.0,
        description="Max time to spend transcribing a single upload.",
    )
    preload: bool = Field(
        default=False,
        description=(
            "When True, load the local Whisper model eagerly at daemon "
            "startup - first request is instant, baseline RAM/VRAM is "
            "higher. Default False because the canonical provider is "
            "``gateway`` (nothing to preload). Set True only when running "
            "``provider=local``."
        ),
    )
    shared_instance: bool = Field(
        default=True,
        description=(
            "One model instance shared across ALL apps and sessions. "
            "Set False only if you need per-session isolation (e.g. "
            "different languages per app) - memory cost scales linearly."
        ),
    )
    max_concurrency: int = Field(
        default=1,
        ge=1,
        le=16,
        description=(
            "Max concurrent transcriptions per model instance. The "
            "faster-whisper model serialises inference internally; "
            "additional concurrency is queued. Bump on a GPU where "
            "multi-stream inference helps."
        ),
    )


class HubConfig(BaseModel):
    """Remote Digitorn Hub settings.

    The hub is a remote service at ``url`` that publishes/searches/serves
    Digitorn application archives. The Hub now accepts the same RS256
    JWT issued by ``auth.digitorn.ai`` (see ``digitorn_hub.auth.central``);
    the daemon forwards the caller's bearer token when downloading an
    archive. The legacy ed25519 daemon-bridge auth was retired.
    """

    url: str = Field(
        default="",
        description=(
            "Base URL of the remote hub (e.g. https://hub.digitorn.ai). "
            "Empty disables hub integration."
        ),
    )
    verify_ssl: bool = True
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)


class WebPreviewConfig(BaseModel):
    """``web_preview`` module — how iframe-attached dev servers are
    addressed by user browsers.

    The agent's dev servers bind to ``127.0.0.1:{port}`` on the daemon
    host. The user's browser needs an URL it can actually reach:

    * **Local dev** (browser + daemon on the same machine) — the
      default ``http://{host}:{port}`` works trivially via loopback.
    * **Cloud / multi-tenant** — the daemon host's loopback isn't
      reachable from the user's browser. Operator must configure a
      public mapping (typically a wildcard subdomain routed by an
      edge proxy: ``*.preview.digitorn.cloud → 127.0.0.1:{port}``).
      Set ``public_url_template`` accordingly:

        public_url_template = "https://preview-{port}.digitorn.cloud"

    Available template fields (Python ``str.format``):
      - ``{host}`` → attachment host (``127.0.0.1`` by default)
      - ``{port}`` → attachment port
      - ``{app_id}`` → deploying app's id
      - ``{session_id}`` → session that created the attachment
      - ``{name}`` → attachment name (``default`` unless overridden)

    Use whichever subset matches your edge proxy's routing. The
    daemon never re-rewrites the URL — it's emitted to the browser
    as-is and the browser connects directly. **No more daemon proxy
    in the hot path.**
    """

    public_url_template: str = Field(
        default="http://{host}:{port}",
        description=(
            "Template for the publicly-reachable URL of a proxy "
            "attachment. Default works for local dev (loopback). "
            "Cloud deploys override to a wildcard subdomain or "
            "exposed port range."
        ),
    )

    enabled: bool = Field(
        default=True,
        description=(
            "Kill switch for web_preview attachments. When false, "
            "PreviewProxy / PreviewStatic refuse new attachments with "
            "a clear error; existing attachments and the /health/web_preview "
            "endpoint stay reachable so an operator can drain in place. "
            "Override at runtime via DIGITORN_WEB_PREVIEW__ENABLED=false "
            "without redeploying."
        ),
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DIGITORN_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    # Deployment mode. Drives credential resolution policy:
    # - `local`  : per_user credentials + grants are honoured. User can
    #   BYOK per-app via the existing grants flow. Falls back to
    #   `system_wide` (= meta credential, env-imported at first boot)
    #   when no per_user grant exists.
    # - `cloud`  : per_user credentials and grants are bypassed. Every
    #   app uses the meta credential (system_wide row in Postgres). The
    #   meta is the single source of truth in hosted; users cannot
    #   override it. Quota is always tracked.
    # Override via `DIGITORN_MODE=cloud` or YAML `mode: cloud`.
    mode: Literal["local", "cloud"] = Field(
        default="local",
        description=(
            "Deployment mode. 'local' = per_user credentials + grants "
            "honoured (BYOK). 'cloud' = always meta credential, no BYOK."
        ),
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    modules: ModulesConfig = Field(default_factory=ModulesConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    app: AppConfig = Field(default_factory=AppConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    default_model: DefaultModelConfig = Field(default_factory=DefaultModelConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    oauth: OAuthConfig = Field(default_factory=OAuthConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    agent_spawn: AgentSpawnConfig = Field(default_factory=AgentSpawnConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    images: ImageConfig = Field(default_factory=ImageConfig)
    web_preview: WebPreviewConfig = Field(default_factory=WebPreviewConfig)
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    hub: HubConfig = Field(default_factory=HubConfig)

    # Out-of-process module workers. Default = disabled (empty list)
    # which preserves the legacy in-process flow byte-for-byte. When
    # ``workers.enabled: true`` and at least one worker entry lists
    # modules, those modules are routed to the worker over HTTP at
    # tool-call time. See ``packages/digitorn/workers/`` for the full
    # design + lifecycle (file leader for cron, monkey-patch wrapper
    # in bootstrap, etc.).
    workers: WorkersConfig = Field(default_factory=WorkersConfig)

    @classmethod
    def load(cls, config_file: Path | None = None) -> Settings:
        """Load settings from YAML files + environment variables.

        Priority (highest wins):
            user config > system config > env vars > defaults

        YAML files are authoritative so ops can pin a value across the
        whole host. Env vars only fill in what the YAML doesn't set.
        """
        data: dict[str, object] = {}

        candidates = [
            Path("/etc/digitorn/config.yaml"),
            Path.home() / ".digitorn" / "config.yaml",
        ]
        if config_file:
            candidates.append(config_file)

        for path in candidates:
            if path.exists():
                try:
                    import yaml
                except ImportError:
                    import warnings
                    warnings.warn(
                        f"Config file {path} found but PyYAML is not installed. "
                        "Install it with: pip install pyyaml",
                        stacklevel=2,
                    )
                    continue

                with path.open(encoding="utf-8") as f:
                    # YAML 1.2 strict bool rules — daemon config has
                    # fields like ``enabled: yes/no`` which under YAML
                    # 1.1 would parse as bool True/False but the user
                    # might mean the strings. Centralise via the loader.
                    from digitorn.core.app.yaml_loader import safe_load_strict
                    loaded = safe_load_strict(f.read()) or {}
                    data.update(loaded)

        return cls(**data)


# Module-level singleton

_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the global settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def override_settings(settings: Settings) -> None:
    """Replace the singleton. Used in tests."""
    global _settings
    _settings = settings
