"""ConnectionStringHandler - databases, caches, message queues."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from digitorn.core.credentials.handler import (
    CredentialHandler,
    ValidationError,
)

logger = logging.getLogger(__name__)


class ConnectionStringHandler(CredentialHandler):
    provider_type = "connection_string"
    allowed_scopes = (
        "system_wide",
        "per_app_shared",
        "per_user",
        "per_app_per_user",
    )

    @classmethod
    def schema_fields(cls) -> list[Any]:
        from digitorn.core.credentials.field_spec import FieldSpec, FieldType
        return [
            FieldSpec(
                name="connection_string",
                label="Connection URL",
                type=FieldType.PASSWORD,
                required=True,
                masked=True,
                help="postgres://user:pass@host:5432/db",
                placeholder="scheme://user:pass@host:port/path",
                inject_path_default="{block}.config.connection_string",
            ),
        ]

    def validate_fields(
        self,
        fields: dict[str, Any],
        schema_fields: list[dict[str, Any]],
    ) -> None:
        # Base class handles required + regex
        super().validate_fields(fields, schema_fields)

        # Extra check: make sure the URL parses
        url_field = self._find_url_field(schema_fields)
        if url_field is None:
            return
        url = (fields or {}).get(url_field)
        if not url:
            return
        try:
            parsed = urlparse(str(url))
        except Exception as exc:
            raise ValidationError(url_field, f"not a valid URL: {exc}")
        if not parsed.scheme or not parsed.hostname:
            raise ValidationError(
                url_field,
                "URL must include scheme and host (e.g. postgres://user:pass@host/db)",
            )

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Attempt a real connection to the database."""
        url = self._extract_url(fields, schema_provider)
        if not url:
            return False, "no URL to test"
        try:
            parsed = urlparse(str(url))
        except Exception as exc:
            return False, f"URL parse failed: {exc}"
        if not parsed.hostname:
            return False, "URL missing host"
        scheme = (parsed.scheme or "").lower()
        return await _probe_db(scheme, str(url))

    def _extract_url(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> str | None:
        url_field = self._find_url_field(
            schema_provider.get("fields") or [],
        )
        if url_field:
            v = (fields or {}).get(url_field)
            if v:
                return str(v)
        # Custom-template path: probe well-known field names.
        for k in ("connection_string", "url", "dsn", "connection_url"):
            v = (fields or {}).get(k)
            if v:
                return str(v)
        return None

    def _find_url_field(
        self,
        schema_fields: list[dict[str, Any]],
    ) -> str | None:
        for f in schema_fields or []:
            if f.get("type") == "connection_string" or f.get("name") in (
                "url", "connection_string", "dsn", "connection_url",
            ):
                return f.get("name")
        return None


async def _probe_db(scheme: str, url: str) -> tuple[bool, str | None]:
    """Open a quick connection through the appropriate driver."""
    import asyncio
    timeout = 8.0
    try:
        if scheme.startswith("postgres"):
            try:
                import asyncpg
            except ImportError:
                return True, "URL parsed (asyncpg not installed; skipping live probe)"
            conn = await asyncio.wait_for(asyncpg.connect(url), timeout)
            try:
                await conn.fetchval("SELECT 1")
            finally:
                await conn.close()
            return True, "Connected (postgres)"
        if scheme.startswith("mysql") or scheme.startswith("mariadb"):
            try:
                import aiomysql
                from urllib.parse import urlparse as _u
            except ImportError:
                return True, "URL parsed (aiomysql not installed; skipping live probe)"
            p = _u(url)
            conn = await asyncio.wait_for(aiomysql.connect(
                host=p.hostname or "localhost",
                port=p.port or 3306,
                user=p.username or "",
                password=p.password or "",
                db=(p.path or "/").lstrip("/") or None,
            ), timeout)
            try:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
            finally:
                conn.close()
            return True, "Connected (mysql)"
        if scheme.startswith("mongodb"):
            try:
                from motor.motor_asyncio import AsyncIOMotorClient
            except ImportError:
                return True, "URL parsed (motor not installed; skipping live probe)"
            client = AsyncIOMotorClient(url, serverSelectionTimeoutMS=int(timeout * 1000))
            try:
                await client.admin.command("ping")
            finally:
                client.close()
            return True, "Connected (mongo)"
        if scheme.startswith("redis"):
            try:
                import redis.asyncio as redis_asyncio
            except ImportError:
                return True, "URL parsed (redis not installed; skipping live probe)"
            client = redis_asyncio.from_url(url, socket_connect_timeout=timeout)
            try:
                pong = await client.ping()
            finally:
                await client.aclose()
            return (pong is True or pong == b"PONG" or pong == "PONG"), \
                "PONG received" if pong else "no PONG"
        # Unknown scheme - URL parses, that's all we can claim.
        return True, f"URL parsed (no live driver for scheme {scheme!r})"
    except asyncio.TimeoutError:
        return False, f"Timeout after {timeout}s"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
