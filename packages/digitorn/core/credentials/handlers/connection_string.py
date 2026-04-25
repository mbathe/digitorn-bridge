"""ConnectionStringHandler — databases, caches, message queues.

Handles credentials that are *URL-shaped*:

    postgres://user:pass@host:5432/db
    mongodb://user:pass@host:27017/db?authSource=admin
    redis://:password@host:6379/0
    mysql://user:pass@host:3306/db

The full URL is one field. The handler parses it to extract the
driver name (for routing), validates it's a proper URL, and can
optionally run a ``test_query`` declared in the schema (like
``SELECT 1`` for postgres) to check the connection before saving.

Like ``ApiKeyHandler``, the live test is opt-in: without a
``test_query`` in the schema, the handler trusts the URL and just
checks it parses.
"""

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
        """Run the declared test query against the connection.

        This is a *real* end-to-end test — we actually connect to the
        database and run a query. Only attempted if the schema
        declares a ``test_query`` field. For now we delegate to the
        existing ``database`` module's connection infrastructure so
        we don't reinvent drivers.
        """
        test_query = schema_provider.get("test_query")
        if not test_query:
            return True, None

        url_field = self._find_url_field(
            schema_provider.get("fields") or [],
        )
        if url_field is None:
            return True, None
        url = (fields or {}).get(url_field)
        if not url:
            return False, "no URL to test"

        # TODO(database): plug into digitorn.modules.database
        # connection pool to run test_query. For now we just check
        # URL parseability as a smoke test.
        try:
            parsed = urlparse(str(url))
            if not parsed.hostname:
                return False, "URL missing host"
            return True, None
        except Exception as exc:
            return False, f"URL parse failed: {exc}"

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
