from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HUB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    host: str = "0.0.0.0"
    port: int = 8001
    log_level: str = "INFO"
    cors_origins: str = "*"

    database_url: str = (
        "postgresql+asyncpg://digitorn:digitorn@localhost:5433/digitorn_hub"
    )

    jwt_secret: str = Field(
        default="change-me-in-production-min-32-chars-long",
        min_length=32,
    )
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 60
    jwt_refresh_ttl_days: int = 30

    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "digitorn-packages"
    s3_presign_ttl_seconds: int = 900

    # Public base URL of THIS Hub deployment. Used to compose icon
    # URLs that point back to our `/api/v1/packages/{pub}/{pkg}/icon`
    # streaming route. When empty we fall back to a relative URL
    # (works for same-origin clients but breaks browser <img> tags
    # served from another origin). Set this to e.g.
    # ``https://hub.digitorn.ai`` in prod.
    hub_public_base_url: str = ""

    max_archive_bytes: int = 100 * 1024 * 1024

    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dim: int = 384
    semantic_max_distance: float = 0.65

    # ── Central auth service (digitorn-auth) ─────────────────────────
    # When set, the Hub accepts RS256 JWTs signed by this service in
    # addition to its own HS256 tokens. Users authenticate against the
    # central, hit the Hub directly with the same Bearer token, and
    # the Hub auto-provisions a local user row by email on first call.
    # Empty = central-auth path disabled (legacy single-machine mode).
    auth_service_url: str = Field(
        default="",
        description=(
            "Base URL of the central digitorn-auth service "
            "(https://auth.digitorn.ai). RS256 tokens issued there "
            "are accepted via JWKS verification."
        ),
    )
    auth_accept_issuers: list[str] = Field(
        default_factory=list,
        description=(
            "Extra `iss` claim values to accept besides auth_service_url."
        ),
    )
    auth_jwks_ttl_seconds: int = Field(
        default=24 * 3600,
        description="How long to cache the JWKS before refreshing.",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
