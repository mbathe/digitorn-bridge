"""API Key auth provider - machine-to-machine authentication.

API keys are hashed (SHA-256) and stored in DB. The raw key is
shown only once at creation time. Keys can be scoped to specific
apps and have expiration dates.

Format: dk_<prefix>_<random> (dk = digitorn key)
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn.core.auth.providers.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)

def generate_api_key() -> tuple[str, str, str]:
    """Generate a new API key.

    Returns (raw_key, key_hash, key_prefix).
    The raw key is shown once. The hash and prefix are stored.
    """
    prefix = secrets.token_hex(4)
    body = secrets.token_hex(24)
    raw_key = f"dk_{prefix}_{body}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return raw_key, key_hash, f"dk_{prefix}"

class APIKeyProvider(AuthProvider):
    """API key authentication for M2M integrations.

    Credentials: {"key": "dk_abcd1234_..."}
    """

    provider_id = "api_key"

    def __init__(self) -> None:
        self._session_factory: Any = None
        self._header_name: str = "X-API-Key"

    async def on_start(self, config: dict[str, Any]) -> None:
        self._header_name = config.get("header", "X-API-Key")

        from digitorn.core.database import get_session_factory
        self._session_factory = get_session_factory()

    async def authenticate(self, credentials: dict[str, Any]) -> AuthResult:
        raw_key = credentials.get("key", "")
        if not raw_key:
            return AuthResult(success=False, error="API key is required")

        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        from digitorn.core.models import APIKey, User

        async with self._session_factory() as session:
            session: AsyncSession

            stmt = select(APIKey).where(
                APIKey.key_hash == key_hash,
                APIKey.is_active == True,
            )
            api_key = (await session.execute(stmt)).scalar_one_or_none()

            if api_key is None:
                return AuthResult(success=False, error="Invalid API key")

            if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
                return AuthResult(success=False, error="API key expired")

            api_key.last_used_at = datetime.now(timezone.utc)
            await session.commit()

            user_stmt = select(User).where(User.id == api_key.user_id)
            user = (await session.execute(user_stmt)).scalar_one_or_none()

            return AuthResult(
                success=True,
                user_id=api_key.user_id,
                external_id=f"apikey:{api_key.key_prefix}",
                email=user.email if user else None,
                display_name=f"API Key ({api_key.name})",
                attributes={"permissions": api_key.permissions, "app_id": api_key.app_id},
            )
