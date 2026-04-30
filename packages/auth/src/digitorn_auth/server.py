"""FastAPI app + lifespan for the digitorn-auth service.

Started via ``digitorn-auth serve`` (CLI) or programmatically by
``create_app()``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from digitorn_auth import __version__
from digitorn_auth.api import admin as admin_router
from digitorn_auth.api import auth as auth_router
from digitorn_auth.api import devices as devices_router
from digitorn_auth.api import jwks as jwks_router
from digitorn_auth.api import oauth as oauth_router
from digitorn_auth.config import get_settings
from digitorn_auth.database import Base, dispose_engine, get_engine, init_engine
from digitorn_auth.jwt import JWTService
from digitorn_auth.service import AuthService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("digitorn-auth starting v=%s issuer=%s", __version__, settings.issuer)

    # 1. DB
    init_engine(settings.database_url, echo=settings.database_echo)
    # Auto-create tables in dev (sqlite). In prod (Postgres shared
    # with the daemon), Alembic owns the schema — this CREATE is a
    # no-op since the tables already exist.
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. JWT — RS256 by default (asymmetric, JWKS-served public key)
    if settings.jwt_algorithm == "RS256":
        jwt_service = JWTService(
            algorithm="RS256",
            access_ttl=settings.access_token_ttl,
            refresh_ttl=settings.refresh_token_ttl,
            private_key_path=settings.jwt_private_key_path,
            public_key_path=settings.jwt_public_key_path,
            kid=settings.jwt_key_id,
        )
    else:
        jwt_service = JWTService(
            algorithm="HS256",
            access_ttl=settings.access_token_ttl,
            refresh_ttl=settings.refresh_token_ttl,
            key_path=settings.jwt_secret_path,
        )

    # 3. AuthService with providers config from env-driven settings
    auth_service = AuthService(jwt_service)
    providers_config: list[dict] = [{"type": "local", "default": True}]

    if settings.oauth_google_client_id:
        providers_config.append({
            "id": "google",
            "type": "oauth2",
            "config": {
                "provider_name": "google",
                "client_id": settings.oauth_google_client_id,
                "client_secret": settings.oauth_google_client_secret,
                "redirect_uri": f"{settings.oauth_redirect_base}/auth/oauth/google/callback",
                "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_url": "https://oauth2.googleapis.com/token",
                "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
                "scope": "openid email profile",
            },
        })

    if settings.oauth_microsoft_client_id:
        tenant = settings.oauth_microsoft_tenant or "common"
        providers_config.append({
            "id": "azure",
            "type": "oauth2",
            "config": {
                "provider_name": "azure",
                "client_id": settings.oauth_microsoft_client_id,
                "client_secret": settings.oauth_microsoft_client_secret,
                "redirect_uri": f"{settings.oauth_redirect_base}/auth/oauth/azure/callback",
                "auth_url": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
                "token_url": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                "userinfo_url": "https://graph.microsoft.com/v1.0/me",
                "scope": "openid email profile",
            },
        })

    await auth_service.start({"providers": providers_config})
    app.state.auth_service = auth_service
    # Cache the JWKS once at startup so the /.well-known/jwks.json
    # endpoint serves a static dict (no per-request key recomputation).
    app.state.jwks = jwt_service.public_jwks()

    yield

    # Shutdown
    logger.info("digitorn-auth stopping")
    await auth_service.stop()
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="digitorn-auth",
        version=__version__,
        description="Central authentication service for the Digitorn ecosystem",
        lifespan=lifespan,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            # Include PUT (admin/account-features upsert) and PATCH
            # (future profile updates). HEAD is auto-included with GET.
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
            # Expose the auth-service custom headers if any future
            # endpoint sets them (rate-limit info, key-rotation hint).
            expose_headers=["X-Request-Id"],
        )

    app.include_router(jwks_router.router)
    app.include_router(auth_router.router)
    app.include_router(oauth_router.router)
    app.include_router(devices_router.router)
    app.include_router(admin_router.router)

    @app.get("/health", tags=["health"])
    async def health():
        return {"ok": True, "service": "digitorn-auth", "version": __version__}

    return app


# Convenience for `uvicorn digitorn_auth.server:app`
app = create_app()
