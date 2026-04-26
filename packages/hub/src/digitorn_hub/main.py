from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .routers import (
    auth, catalog, daemon_bridge, health, packages, publishers,
    reports, reviews, stats,
)
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
    yield
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
    app.include_router(daemon_bridge.router, prefix="/api/v1")
    app.include_router(publishers.router, prefix="/api/v1")
    app.include_router(packages.router, prefix="/api/v1")
    app.include_router(catalog.router, prefix="/api/v1")
    app.include_router(reviews.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(stats.router, prefix="/api/v1")

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": "digitorn-hub", "version": __version__}

    return app


app = create_app()
