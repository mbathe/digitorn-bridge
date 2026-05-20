"""Credential handler framework - one pluggable class per provider type."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Base class


class ValidationError(Exception):
    """Field validation failed (regex mismatch, required field empty, etc.)."""

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


class HandlerNotAvailable(Exception):
    """Raised when a handler's operation requires external setup that"""


class CredentialHandler:
    """Abstract base class. Every provider_type gets a subclass."""

    provider_type: str = "abstract"

    # Default: all four scopes allowed. Subclasses (OAuth2) restrict.
    allowed_scopes: tuple[str, ...] = (
        "system_wide",
        "per_app_shared",
        "per_user",
        "per_app_per_user",
    )

    @classmethod
    def schema_fields(cls) -> list[Any]:
        """Return the FieldSpec list for this handler. Subclasses"""
        return []


    def validate_fields(
        self,
        fields: dict[str, Any],
        schema_fields: list[dict[str, Any]],
    ) -> None:
        """Validate each submitted field against its schema entry."""
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


    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Try to connect to the external service with these credentials."""
        return True, None


    async def refresh(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> dict[str, Any]:
        """Refresh an expired credential (OAuth) or re-validate it (API key)."""
        return credential


    async def revoke(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> None:
        """Tell the external service to kill this credential."""
        return None


    async def on_credential_filled(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
        ctx: Any,
    ) -> None:
        """Called after the user fills the credential for the first time."""
        return None

    async def on_credential_removed(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
        ctx: Any,
    ) -> None:
        """Called right before a credential is deleted."""
        return None


# Registry


class HandlerRegistry:
    """Global registry mapping `provider_type` → handler instance."""

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


default_registry = HandlerRegistry()


# Convenience helpers


def now_utc() -> datetime:
    """Return a timezone-aware UTC datetime - use this everywhere"""
    return datetime.now(timezone.utc)
