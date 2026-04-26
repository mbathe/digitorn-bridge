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

    # ── Daemon-bridge auth (auto-provisioning of Hub sessions) ───────
    # When False the `/auth/daemon-bridge` endpoint returns 404 even
    # if a trusted daemon presents a valid signature. Default-off so
    # rolling out the feature is a single env-var flip.
    enable_daemon_bridge: bool = False
    # Reject signed payloads whose `ts` is more than this many seconds
    # away from server time (in either direction). 60s is generous
    # enough for clock drift but short enough to make replay windows
    # tiny.
    daemon_bridge_max_clock_skew_seconds: int = 60
    # Validity of the Hub session token issued by the bridge. The
    # daemon caches it; once expired it will re-bridge transparently.
    daemon_bridge_session_ttl_minutes: int = 60

    @property
    def cors_origins_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
