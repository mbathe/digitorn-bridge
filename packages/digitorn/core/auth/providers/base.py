"""Base auth provider interface.

All auth providers implement this protocol. The AuthService
dispatches to the configured providers at login time.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class AuthResult:
    """Result of an authentication attempt."""

    success: bool
    user_id: str | None = None
    external_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    attributes: dict[str, Any] | None = None
    error: str | None = None


class AuthProvider(ABC):
    """Base interface for authentication providers.

    Implementations: LocalProvider, LDAPProvider, OAuth2Provider, APIKeyProvider.
    """

    provider_id: str = "base"

    @abstractmethod
    async def authenticate(self, credentials: dict[str, Any]) -> AuthResult:
        """Verify credentials and return an AuthResult.

        The credentials dict varies by provider:
        - local: {"username": "...", "password": "..."}
        - ldap: {"username": "...", "password": "..."}
        - oauth2: {"code": "...", "redirect_uri": "..."}
        - api_key: {"key": "..."}
        """
        ...

    async def on_start(self, config: dict[str, Any]) -> None:
        """Initialize the provider with its configuration."""
        pass

    async def on_stop(self) -> None:
        """Cleanup resources."""
        pass
