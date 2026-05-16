from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .auth.central import CentralAuthClient
from .routers import (
    auth, catalog, categories, health, packages, publishers,
    reports, reviews, stats,
)
from .routers import mcp_featured as mcp_featured_router
from .routers import mcp_registry as mcp_registry_router
from .settings import get_settings

logger = structlog.get_logger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)
    logger.info("hub.start", version=__version__, port=settings.port)

    # Pre-warm the central auth JWKS cache so the first request that
    # carries an RS256 token doesn't pay the discovery + key-fetch
    # cost. Tolerates a network failure - the verifier retries lazily
    # on the next call.
    if settings.auth_service_url:
        client = CentralAuthClient(
            issuer=settings.auth_service_url,
            jwks_ttl=settings.auth_jwks_ttl_seconds,
            accept_issuers=settings.auth_accept_issuers,
        )
        try:
            await client.start()
            app.state.central_auth = client
            logger.info(
                "central_auth_enabled",
                issuer=settings.auth_service_url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "central_auth_init_failed",
                issuer=settings.auth_service_url,
                error=str(exc),
            )
            app.state.central_auth = None
    else:
        app.state.central_auth = None

    # MCP registry ingester — periodic background fetch from
    # registry.modelcontextprotocol.io. Fire-and-forget; the hub
    # serves stale cache fine if the upstream is down.
    from .db import SessionLocal
    from .mcp_registry_ingester import start_periodic_loop
    try:
        task = start_periodic_loop(SessionLocal)
        app.state.mcp_registry_task = task
        logger.info("mcp_registry_ingester_started")
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_registry_ingester_start_failed", error=str(exc))
        app.state.mcp_registry_task = None

    yield

    task = getattr(app.state, "mcp_registry_task", None)
    if task is not None and not task.done():
        task.cancel()

    logger.info("hub.stop")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Digitorn Hub",
        version=__version__,
        description=(
            "Hub server for publishing, searching and installing "
            "Digitorn applications."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(publishers.router, prefix="/api/v1")
    app.include_router(packages.router, prefix="/api/v1")
    app.include_router(catalog.router, prefix="/api/v1")
    app.include_router(categories.router, prefix="/api/v1")
    app.include_router(reviews.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(stats.router, prefix="/api/v1")
    app.include_router(mcp_registry_router.router, prefix="/api/v1")
    app.include_router(mcp_featured_router.router, prefix="/api/v1")

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": "digitorn-hub", "version": __version__}

    return app


app = create_app()
