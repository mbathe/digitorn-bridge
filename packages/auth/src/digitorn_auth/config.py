"""Configuration for digitorn-auth.

Env-driven via pydantic-settings. Variables prefixed with
``DIGITORN_AUTH_`` to keep them isolated from the daemon's
``DIGITORN_`` namespace.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    """Authentication service settings.

    Override via env vars (prefix ``DIGITORN_AUTH_``) or a ``.env`` file
    next to the package.
    """

    model_config = SettingsConfigDict(
        env_prefix="DIGITORN_AUTH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Service identity ────────────────────────────────────────────
    issuer: str = Field(
        default="https://auth.digitorn.ai",
        description=(
            "JWT `iss` claim. Daemons must use this value when verifying "
            "tokens. In dev: http://127.0.0.1:8001."
        ),
    )

    # ── Database ────────────────────────────────────────────────────
    database_url: str = Field(
        default="sqlite+aiosqlite:///./digitorn-auth.db",
        description=(
            "Async SQLAlchemy URL. Use postgresql+asyncpg://... in prod, "
            "sqlite+aiosqlite:///./auth.db in dev."
        ),
    )
    database_echo: bool = False

    # ── JWT ─────────────────────────────────────────────────────────
    jwt_algorithm: str = Field(
        default="RS256",
        description=(
            "Signing algorithm. RS256 (asymmetric) is the default — "
            "the auth service holds the private key, daemons verify "
            "with the public key fetched from /.well-known/jwks.json. "
            "Set to HS256 for legacy / single-machine dev (shared secret)."
        ),
    )
    jwt_private_key_path: Path = Field(
        default_factory=lambda: Path.home() / ".digitorn" / "auth-private.pem",
        description="RSA private key (PEM). Auto-generated 2048-bit on first start.",
    )
    jwt_public_key_path: Path = Field(
        default_factory=lambda: Path.home() / ".digitorn" / "auth-public.pem",
        description="RSA public key (PEM). Auto-generated alongside the private key.",
    )
    jwt_key_id: str = Field(
        default="auth-2026-04",
        description=(
            "JWKS `kid` for the active key. Bump when rotating keys "
            "(both old and new can coexist in the JWKS until all live "
            "tokens have expired)."
        ),
    )
    # Legacy HS256 path — only used when jwt_algorithm == 'HS256'.
    jwt_secret_path: Path = Field(
        default_factory=lambda: Path.home() / ".digitorn" / "jwt.key",
        description="Path to the HS256 secret file. Used only when jwt_algorithm=HS256.",
    )
    access_token_ttl: int = Field(default=900, description="Access token TTL in seconds (15 min).")
    refresh_token_ttl: int = Field(
        default=7 * 24 * 3600, description="Refresh token TTL in seconds (7 days).",
    )

    # ── Device pairing ──────────────────────────────────────────────
    device_token_ttl: int = Field(
        default=90 * 24 * 3600,
        description="Long-lived device token TTL (90 days).",
    )
    device_token_rolling_refresh_threshold: int = Field(
        default=30 * 24 * 3600,
        description=(
            "If the device_token expires within this many seconds, the "
            "revalidate endpoint mints a fresh one. Keeps active devices "
            "indefinitely valid as long as they connect at least once "
            "every (ttl - threshold) days."
        ),
    )

    # ── OAuth providers (optional, env-driven) ─────────────────────
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""
    oauth_microsoft_client_id: str = ""
    oauth_microsoft_client_secret: str = ""
    oauth_microsoft_tenant: str = "common"
    oauth_redirect_base: str = Field(
        default="http://127.0.0.1:8001",
        description="Base URL for OAuth callbacks (must match what's registered with the provider).",
    )
    oauth_web_origin: str = Field(
        default="",
        description=(
            "Web frontend origin where the OAuth callback bounces the "
            "user after a successful login (e.g. https://app.digitorn.ai). "
            "This is where the SPA hosts the /auth/oauth-return page that "
            "reads the JWT from the URL fragment. Distinct from "
            "oauth_redirect_base which is the auth service's own URL "
            "registered with the OAuth provider. Falls back to the first "
            "https origin in cors_origins when empty."
        ),
    )

    # ── CORS ────────────────────────────────────────────────────────
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            # Local dev
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            # Production web clients
            "https://app.digitorn.ai",
            "https://digitorn.ai",
            "https://www.digitorn.ai",
            # Hub front-end (same domain as the central, but a
            # cross-origin browser fetch from app.digitorn.ai → hub
            # for asset previews still needs to be allowed).
            "https://hub.digitorn.ai",
        ],
        description=(
            "Origins allowed to call /auth/* endpoints from the browser. "
            "Override via DIGITORN_AUTH_CORS_ORIGINS='[\"https://...\"]' "
            "to add custom hosts (enterprise / staging)."
        ),
    )

    # ── Avatar storage ──────────────────────────────────────────────
    avatar_dir: Path = Field(
        default_factory=lambda: Path.home() / ".digitorn" / "avatars",
        description=(
            "Directory where uploaded avatars are stored. On Fly this "
            "points inside the persistent volume mounted at "
            "/home/auth/.digitorn so avatars survive machine recycling."
        ),
    )
    avatar_max_bytes: int = Field(
        default=5 * 1024 * 1024,
        description="Reject avatar uploads larger than this many bytes (5 MB default).",
    )

    # ── Misc ────────────────────────────────────────────────────────
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> AuthSettings:
    """Singleton accessor.

    Cached so settings are loaded ONCE at process start. To override at
    test time, clear the cache: ``get_settings.cache_clear()``.
    """
    return AuthSettings()


def override_settings(**kwargs: Any) -> AuthSettings:
    """Replace the cached settings with overrides. Test-only helper."""
    get_settings.cache_clear()
    fresh = AuthSettings(**kwargs)
    # Re-prime the cache so subsequent calls return the override.
    get_settings.cache_clear()
    setattr(get_settings, "__wrapped__", lambda: fresh)
    return fresh
