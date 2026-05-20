"""SecretStore - per-application encrypted secret storage."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select

from digitorn.core.crypto import decrypt_value, encrypt_value

logger = logging.getLogger(__name__)


class SecretStore:
    """Backend-agnostic per-app secret management."""

    async def set_secret(self, app_id: str, key: str, value: str) -> None:
        """Store (or update) an encrypted secret for an application."""
        from digitorn.core.database import get_session
        from digitorn.core.models import AppSecret

        encrypted = encrypt_value(value)

        async for session in get_session():
            stmt = select(AppSecret).where(
                AppSecret.app_id == app_id,
                AppSecret.key == key,
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                existing.encrypted_value = encrypted
            else:
                secret = AppSecret(
                    app_id=app_id,
                    key=key,
                    encrypted_value=encrypted,
                )
                session.add(secret)

            await session.commit()
            logger.info("secret_set app=%s key=%s", app_id, key)

    async def get_secret(self, app_id: str, key: str) -> str | None:
        """Get a decrypted secret value, or None if not found."""
        from digitorn.core.database import get_session
        from digitorn.core.models import AppSecret

        async for session in get_session():
            stmt = select(AppSecret).where(
                AppSecret.app_id == app_id,
                AppSecret.key == key,
            )
            result = await session.execute(stmt)
            secret = result.scalar_one_or_none()
            if secret is None:
                return None
            return decrypt_value(secret.encrypted_value)

        return None  # pragma: no cover

    async def delete_secret(self, app_id: str, key: str) -> bool:
        """Delete a secret. Returns True if it existed."""
        from digitorn.core.database import get_session
        from digitorn.core.models import AppSecret

        async for session in get_session():
            stmt = delete(AppSecret).where(
                AppSecret.app_id == app_id,
                AppSecret.key == key,
            )
            result = await session.execute(stmt)
            await session.commit()
            deleted = result.rowcount > 0  # type: ignore[operator]
            if deleted:
                logger.info("secret_deleted app=%s key=%s", app_id, key)
            return deleted

        return False  # pragma: no cover

    async def list_secrets(self, app_id: str) -> list[str]:
        """List secret key names for an app (no values returned)."""
        from digitorn.core.database import get_session
        from digitorn.core.models import AppSecret

        async for session in get_session():
            stmt = (
                select(AppSecret.key)
                .where(AppSecret.app_id == app_id)
                .order_by(AppSecret.key)
            )
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

        return []  # pragma: no cover

    async def get_all(self, app_id: str) -> dict[str, str]:
        """Get all decrypted secrets for an app as a dict."""
        from digitorn.core.database import get_session
        from digitorn.core.models import AppSecret

        async for session in get_session():
            stmt = select(AppSecret).where(AppSecret.app_id == app_id)
            result = await session.execute(stmt)
            secrets = result.scalars().all()
            return {s.key: decrypt_value(s.encrypted_value) for s in secrets}

        return {}  # pragma: no cover
