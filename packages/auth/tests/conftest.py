"""Shared pytest fixtures.

Each test gets a fresh in-memory SQLite + a fresh FastAPI app so the
state is fully isolated and parallel-safe.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture
async def app_and_client(tmp_path: Path):
    # Clean env to prevent any developer .env from leaking in
    for k in list(os.environ):
        if k.startswith("DIGITORN_AUTH_"):
            del os.environ[k]

    secret_path = tmp_path / "jwt.key"
    secret_path.write_text(secrets.token_hex(32))

    db_path = tmp_path / "auth-test.db"

    os.environ["DIGITORN_AUTH_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["DIGITORN_AUTH_JWT_SECRET_PATH"] = str(secret_path)
    os.environ["DIGITORN_AUTH_ISSUER"] = "http://test.local"
    os.environ["DIGITORN_AUTH_ACCESS_TOKEN_TTL"] = "900"
    os.environ["DIGITORN_AUTH_REFRESH_TOKEN_TTL"] = "604800"
    os.environ["DIGITORN_AUTH_DEVICE_TOKEN_TTL"] = "60"  # short for refresh-test
    os.environ["DIGITORN_AUTH_DEVICE_TOKEN_ROLLING_REFRESH_THRESHOLD"] = "30"

    # Reset cached settings
    from digitorn_auth.config import get_settings
    get_settings.cache_clear()

    from digitorn_auth.server import create_app
    app = create_app()

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test.local") as client:
            yield app, client
