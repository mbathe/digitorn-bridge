"""Daemon-side user-bound storage: OAuth tokens + session bindings."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update

from digitorn.core.crypto import decrypt_value as _decrypt_token
from digitorn.core.crypto import encrypt_value as _encrypt_token

logger = logging.getLogger(__name__)


class OAuthTokenInfo:
    """Lightweight token record (decrypted)."""

    __slots__ = (
        "user_id", "provider", "scope", "access_token",
        "refresh_token", "token_type", "expires_at",
    )

    def __init__(self, **kwargs: Any) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        now = datetime.now(timezone.utc)
        if isinstance(self.expires_at, datetime):
            return now >= self.expires_at
        return False


class UserStore:
    """OAuth token storage + session-to-user binding for the daemon."""


    async def bind_session(self, session_id: str, user_id: str) -> bool:
        """Link a session_id to a user. Returns True on success."""
        from digitorn.core.database import get_session
        from digitorn.core.models import UserSession

        async for session in get_session():
            stmt = (
                update(UserSession)
                .where(UserSession.session_id == session_id)
                .values(user_id=user_id)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0  # type: ignore[return-value]

        return False  # pragma: no cover

    async def get_user_id_for_session(self, session_id: str) -> str | None:
        """Return the user_id bound to a session, or None."""
        from digitorn.core.database import get_session
        from digitorn.core.models import UserSession

        async for session in get_session():
            stmt = select(UserSession.user_id).where(
                UserSession.session_id == session_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

        return None  # pragma: no cover

    async def resolve_user_for_session(self, session_id: str) -> dict[str, Any] | None:
        user_id = await self.get_user_id_for_session(session_id)
        if not user_id:
            return None
        return {"user_id": user_id}


    async def store_token(
        self,
        user_id: str,
        provider: str,
        access_token: str,
        refresh_token: str | None = None,
        *,
        scope: str = "",
        token_type: str = "bearer",
        expires_at: datetime | None = None,
    ) -> None:
        """Store (or update) an OAuth token for a user+provider."""
        from digitorn.core.database import get_session
        from digitorn.core.models import UserOAuthToken

        enc_access = _encrypt_token(access_token)
        enc_refresh = _encrypt_token(refresh_token) if refresh_token else None

        async for session in get_session():
            stmt = select(UserOAuthToken).where(
                UserOAuthToken.user_id == user_id,
                UserOAuthToken.provider == provider,
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                existing.access_token_enc = enc_access
                existing.refresh_token_enc = enc_refresh
                existing.scope = scope
                existing.token_type = token_type
                existing.expires_at = expires_at
            else:
                token = UserOAuthToken(
                    user_id=user_id,
                    provider=provider,
                    scope=scope,
                    access_token_enc=enc_access,
                    refresh_token_enc=enc_refresh,
                    token_type=token_type,
                    expires_at=expires_at,
                )
                session.add(token)

            await session.commit()

    async def get_token(
        self, user_id: str, provider: str
    ) -> OAuthTokenInfo | None:
        """Get decrypted OAuth token for a user+provider."""
        from digitorn.core.database import get_session
        from digitorn.core.models import UserOAuthToken

        async for session in get_session():
            stmt = select(UserOAuthToken).where(
                UserOAuthToken.user_id == user_id,
                UserOAuthToken.provider == provider,
            )
            result = await session.execute(stmt)
            tok = result.scalar_one_or_none()
            if not tok:
                return None

            return OAuthTokenInfo(
                user_id=tok.user_id,
                provider=tok.provider,
                scope=tok.scope,
                access_token=_decrypt_token(tok.access_token_enc),
                refresh_token=(
                    _decrypt_token(tok.refresh_token_enc)
                    if tok.refresh_token_enc
                    else None
                ),
                token_type=tok.token_type,
                expires_at=tok.expires_at,
            )

        return None  # pragma: no cover

    async def revoke_token(self, user_id: str, provider: str) -> bool:
        """Delete an OAuth token. Returns True if it existed."""
        from digitorn.core.database import get_session
        from digitorn.core.models import UserOAuthToken

        async for session in get_session():
            stmt = select(UserOAuthToken).where(
                UserOAuthToken.user_id == user_id,
                UserOAuthToken.provider == provider,
            )
            result = await session.execute(stmt)
            tok = result.scalar_one_or_none()
            if tok:
                await session.delete(tok)
                await session.commit()
                return True
            return False

        return False  # pragma: no cover

    async def refresh_token_if_needed(
        self,
        user_id: str,
        provider: str,
        *,
        refresh_callback: Any | None = None,
        buffer_seconds: int = 300,
    ) -> OAuthTokenInfo | None:
        """Get a valid access token, refreshing if expired."""
        token = await self.get_token(user_id, provider)
        if token is None:
            return None

        if token.expires_at is not None:
            now = datetime.now(timezone.utc)
            threshold = token.expires_at
            if isinstance(threshold, datetime):
                threshold = threshold - timedelta(seconds=buffer_seconds)
            if now >= threshold:
                if refresh_callback is not None and token.refresh_token:
                    try:
                        new_data = await refresh_callback(token.refresh_token)
                        new_access = new_data.get("access_token", "")
                        new_refresh = new_data.get("refresh_token", token.refresh_token)
                        new_scope = new_data.get("scope", token.scope or "")

                        new_expires_at: datetime | None = None
                        if "expires_in" in new_data:
                            try:
                                secs = int(new_data["expires_in"])
                                new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=secs)
                            except (ValueError, TypeError):
                                pass

                        await self.store_token(
                            user_id, provider, new_access, new_refresh,
                            scope=new_scope,
                            token_type=token.token_type or "bearer",
                            expires_at=new_expires_at,
                        )
                        return await self.get_token(user_id, provider)
                    except Exception:
                        logger.warning(
                            "oauth_refresh_failed user=%s provider=%s",
                            user_id, provider, exc_info=True,
                        )
                        return None
                else:
                    return None

        return token

    async def find_token_by_provider(
        self, provider: str
    ) -> OAuthTokenInfo | None:
        """Find the most recent valid token for a provider (any user)."""
        from digitorn.core.database import get_session
        from digitorn.core.models import UserOAuthToken

        async for session in get_session():
            stmt = (
                select(UserOAuthToken)
                .where(UserOAuthToken.provider == provider)
                .order_by(UserOAuthToken.updated_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            tok = result.scalar_one_or_none()
            if not tok:
                return None

            token_info = OAuthTokenInfo(
                user_id=tok.user_id,
                provider=tok.provider,
                scope=tok.scope,
                access_token=_decrypt_token(tok.access_token_enc),
                refresh_token=(
                    _decrypt_token(tok.refresh_token_enc)
                    if tok.refresh_token_enc
                    else None
                ),
                token_type=tok.token_type,
                expires_at=tok.expires_at,
            )

            if token_info.is_expired and not token_info.refresh_token:
                return None

            return token_info

        return None  # pragma: no cover
