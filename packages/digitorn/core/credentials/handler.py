"""Credential handler framework — one pluggable class per provider type.

A **CredentialHandler** knows how to:

- Validate fields against the schema (regex, required, format)
- Test a filled credential against the live service (optional)
- Refresh an expired credential (OAuth flow) or re-validate it (API key)
- Render the schema fragment that the Flutter form needs
- React to lifecycle hooks (app install, app uninstall)

Adding support for a new provider type = writing a new handler class
and registering it. The runtime, routes, and store don't need any
change. This is the extension point for the future Digitorn Hub: a
community package can ship its own handler.

Six handlers ship with the daemon:

- ``api_key``         — OpenAI, Anthropic, DeepSeek, any plain-key API
- ``multi_field``     — Slack (bot_token + signing_secret + app_token)
- ``oauth2``          — Notion, Google, GitHub, Slack OAuth
- ``connection_string`` — postgres://, mongodb://, redis://
- ``mcp_server``      — stdio/http MCP servers (with lifecycle)
- ``custom``          — catch-all, declared by the YAML

Handlers are *stateless*: they receive the Credential dict as input
and return a new Credential dict as output. They **never** write to
the DB themselves — the store does that. This keeps the layering
clean and the handlers easy to test.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Base class
# ────────────────────────────────────────────────────────────────────


class ValidationError(Exception):
    """Field validation failed (regex mismatch, required field empty, etc.)."""

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


class HandlerNotAvailable(Exception):
    """Raised when a handler's operation requires external setup that
    the current daemon install doesn't provide (e.g. no OAuth client
    registered for Notion, or no cryptography for signed JWT)."""


class CredentialHandler:
    """Abstract base class. Every provider_type gets a subclass.

    Subclasses override the methods they need. The base class
    provides sensible defaults: ``validate_fields`` does regex +
    required checking, ``test_live_connection`` is a no-op returning
    True, ``refresh`` is a no-op returning the credential unchanged.
    """

    provider_type: str = "abstract"

    # ── Schema validation ────────────────────────────────────────

    def validate_fields(
        self,
        fields: dict[str, Any],
        schema_fields: list[dict[str, Any]],
    ) -> None:
        """Validate each submitted field against its schema entry.

        Raises ``ValidationError`` on the first failure. The default
        implementation checks ``required`` and ``validation_regex``.
        Subclasses can override for type-specific checks (e.g.
        checking that a connection_string url parses as a valid URL).
        """
        provided = {k: v for k, v in (fields or {}).items() if v not in (None, "")}
        for field_def in schema_fields or []:
            name = field_def.get("name")
            if not name:
                continue
            required = field_def.get("required", False)
            regex = field_def.get("validation_regex")
            value = provided.get(name)
            if required and (value is None or value == ""):
                raise ValidationError(name, "required field is empty")
            if value is not None and regex:
                if not re.match(regex, str(value)):
                    raise ValidationError(
                        name,
                        f"value does not match required pattern {regex!r}",
                    )

    # ── Live testing (optional, external I/O) ────────────────────

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Try to connect to the external service with these credentials.

        Returns ``(ok, error_message)``. The default is (True, None)
        so a handler that hasn't overridden this method effectively
        skips the live test and trusts the fields.

        Subclasses that can actually test (API key against a
        ``/me`` endpoint, connection_string with ``SELECT 1``) should
        override.
        """
        return True, None

    # ── Refresh (for expiring creds) ─────────────────────────────

    async def refresh(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> dict[str, Any]:
        """Refresh an expired credential (OAuth) or re-validate it (API key).

        Takes the current credential dict (as returned by
        ``CredentialStore.get_credential(decrypt=True)``) and returns
        a new credential dict with updated ``fields``, ``status``,
        ``expires_at``.

        Default: no-op, returns the input unchanged.
        """
        return credential

    # ── Revocation (tell the external service to kill this cred) ─

    async def revoke(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> None:
        """Tell the external service to kill this credential.

        For OAuth: hit the revocation endpoint.
        For API key: no-op (the user has to revoke manually in the
        provider's dashboard).
        Default: no-op.
        """
        return None

    # ── Lifecycle hooks ──────────────────────────────────────────

    async def on_credential_filled(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
        ctx: Any,
    ) -> None:
        """Called after the user fills the credential for the first time.

        Useful for types that need to **spawn** something: MCP server
        handlers start their process here, OAuth handlers might
        prefetch a ``/me`` call to populate display_metadata.

        ``ctx`` is an opaque dict-like with references the handler
        might need (mcp_pool, logger, …). Kept loose so we don't
        couple handlers to specific service types.
        """
        return None

    async def on_credential_removed(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
        ctx: Any,
    ) -> None:
        """Called right before a credential is deleted.

        MCP handlers stop the process here. OAuth handlers may
        revoke the token.
        """
        return None


# ────────────────────────────────────────────────────────────────────
# Registry
# ────────────────────────────────────────────────────────────────────


class HandlerRegistry:
    """Global registry mapping ``provider_type`` → handler instance."""

    def __init__(self) -> None:
        self._handlers: dict[str, CredentialHandler] = {}

    def register(self, handler: CredentialHandler) -> None:
        if not handler.provider_type:
            raise ValueError("handler must set provider_type")
        if handler.provider_type in self._handlers:
            logger.debug(
                "handler for %s replaced", handler.provider_type,
            )
        self._handlers[handler.provider_type] = handler

    def get(self, provider_type: str) -> CredentialHandler:
        handler = self._handlers.get(provider_type)
        if handler is None:
            # Fallback: the 'custom' handler accepts anything.
            handler = self._handlers.get("custom")
        if handler is None:
            raise KeyError(
                f"no handler registered for provider_type={provider_type!r} "
                f"and no 'custom' fallback"
            )
        return handler

    def known_types(self) -> list[str]:
        return sorted(self._handlers.keys())


# Module-level default registry. Handlers populate it on import from
# ``handlers/__init__.py`` — callers can also instantiate their own
# registry for tests.
default_registry = HandlerRegistry()


# ────────────────────────────────────────────────────────────────────
# Convenience helpers
# ────────────────────────────────────────────────────────────────────


def now_utc() -> datetime:
    """Return a timezone-aware UTC datetime — use this everywhere
    instead of ``datetime.utcnow()`` (which is deprecated and naive)."""
    return datetime.now(timezone.utc)
