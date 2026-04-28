"""UserStore - unified user management with OAuth token storage.

Provides CRUD operations for users, OAuth token management, and
session-to-user binding.  Works with the async SQLAlchemy ORM
(same engine as the rest of the daemon).

Usage::

    store = UserStore()
    user = await store.create_user("my-app", "local", "user@example.com",
                                    email="user@example.com",
                                    display_name="Alice")
    await store.bind_session("session-123", user.id)
    resolved = await store.resolve_user_for_session("session-123")

Token encryption uses Fernet if ``cryptography`` is installed,
otherwise falls back to base64 obfuscation (dev-only).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from digitorn.core.crypto import decrypt_value as _decrypt_token
from digitorn.core.crypto import encrypt_value as _encrypt_token

logger = logging.getLogger(__name__)


class UserInfo:
    """Lightweight user record returned by UserStore methods."""

    __slots__ = (
        "id", "external_id", "provider", "app_id", "email",
        "display_name", "phone", "avatar_url", "attributes",
        "is_active", "created_at", "updated_at", "last_seen_at",
    )

    def __init__(self, **kwargs: Any) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_orm(cls, obj: Any) -> UserInfo:
        return cls(
            id=obj.id,
            external_id=obj.external_id,
            provider=obj.provider,
            app_id=obj.app_id,
            email=obj.email,
            display_name=obj.display_name,
            phone=obj.phone,
            avatar_url=obj.avatar_url,
            attributes=obj.attributes or {},
            is_active=obj.is_active,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
            last_seen_at=obj.last_seen_at,
        )


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
    """Backend-agnostic unified user management.

    Uses the daemon's async SQLAlchemy engine for durable storage.
    All methods are async and require ``init_db()`` to have been called.
    """


    async def create_user(
        self,
        app_id: str | None,
        provider: str,
        external_id: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
        phone: str | None = None,
        avatar_url: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> UserInfo:
        """Create a new user. Raises ValueError on duplicate."""
        from digitorn.core.database import get_session
        from digitorn.core.models import User

        async for session in get_session():
            user = User(
                external_id=external_id,
                provider=provider,
                app_id=app_id,
                email=email,
                display_name=display_name,
                phone=phone,
                avatar_url=avatar_url,
                attributes=attributes or {},
            )
            session.add(user)
            try:
                await session.commit()
                await session.refresh(user)
            except IntegrityError:
                await session.rollback()
                raise ValueError(
                    f"User already exists: provider={provider}, "
                    f"external_id={external_id}, app_id={app_id}"
                )
            return UserInfo.from_orm(user)

        raise RuntimeError("Database session not available")  # pragma: no cover

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Get user by internal ID."""
        from digitorn.core.database import get_session
        from digitorn.core.models import User

        async for session in get_session():
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            return UserInfo.from_orm(user) if user else None

        return None  # pragma: no cover

    async def find_user(
        self, app_id: str | None, provider: str, external_id: str
    ) -> UserInfo | None:
        """Find user by provider + external_id (scoped by app)."""
        from digitorn.core.database import get_session
        from digitorn.core.models import User

        async for session in get_session():
            stmt = select(User).where(
                User.provider == provider,
                User.external_id == external_id,
            )
            if app_id is not None:
                stmt = stmt.where(User.app_id == app_id)
            else:
                stmt = stmt.where(User.app_id.is_(None))
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            return UserInfo.from_orm(user) if user else None

        return None  # pragma: no cover

    async def find_by_email(
        self, app_id: str | None, email: str
    ) -> UserInfo | None:
        """Find user by email (scoped by app, or cross-app if app_id=None)."""
        from digitorn.core.database import get_session
        from digitorn.core.models import User

        async for session in get_session():
            stmt = select(User).where(User.email == email, User.is_active.is_(True))
            if app_id is not None:
                stmt_app = stmt.where(User.app_id == app_id)
                result = await session.execute(stmt_app)
                user = result.scalar_one_or_none()
                if user:
                    return UserInfo.from_orm(user)
                stmt_global = stmt.where(User.app_id.is_(None))
                result = await session.execute(stmt_global)
                user = result.scalar_one_or_none()
                return UserInfo.from_orm(user) if user else None
            else:
                stmt = stmt.where(User.app_id.is_(None))
                result = await session.execute(stmt)
                user = result.scalar_one_or_none()
                return UserInfo.from_orm(user) if user else None

        return None  # pragma: no cover

    async def find_by_session(self, session_id: str) -> UserInfo | None:
        """Resolve the user linked to a session_id."""
        from digitorn.core.database import get_session
        from digitorn.core.models import User, UserSession

        async for session in get_session():
            stmt = (
                select(User)
                .join(UserSession, UserSession.user_id == User.id)
                .where(UserSession.session_id == session_id)
            )
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            return UserInfo.from_orm(user) if user else None

        return None  # pragma: no cover

    async def update_user(self, user_id: str, **fields: Any) -> UserInfo | None:
        """Update user fields. Returns updated user or None if not found."""
        from digitorn.core.database import get_session
        from digitorn.core.models import User

        allowed = {
            "email", "display_name", "phone", "avatar_url",
            "attributes", "is_active", "external_id", "last_seen_at",
        }
        update_fields = {k: v for k, v in fields.items() if k in allowed}
        if not update_fields:
            return await self.get_user(user_id)

        async for session in get_session():
            stmt = (
                update(User)
                .where(User.id == user_id)
                .values(**update_fields)
            )
            await session.execute(stmt)
            await session.commit()
            return await self.get_user(user_id)

        return None  # pragma: no cover

    async def list_users(
        self,
        app_id: str | None,
        *,
        offset: int = 0,
        limit: int = 50,
        active_only: bool = True,
    ) -> list[UserInfo]:
        """List users for an app (or cross-app if app_id=None)."""
        from digitorn.core.database import get_session
        from digitorn.core.models import User

        async for session in get_session():
            stmt = select(User)
            if app_id is not None:
                stmt = stmt.where(
                    (User.app_id == app_id) | (User.app_id.is_(None))
                )
            else:
                stmt = stmt.where(User.app_id.is_(None))
            if active_only:
                stmt = stmt.where(User.is_active.is_(True))
            stmt = stmt.order_by(User.created_at.desc()).offset(offset).limit(limit)
            result = await session.execute(stmt)
            return [UserInfo.from_orm(u) for u in result.scalars().all()]

        return []  # pragma: no cover


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

    async def resolve_user_for_session(self, session_id: str) -> UserInfo | None:
        """Resolve user from session_id. Alias for find_by_session."""
        return await self.find_by_session(session_id)


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
        """Get a valid access token, refreshing if expired.

        If the token is expired (or will expire within ``buffer_seconds``),
        and a ``refresh_callback`` is provided, it will be called to obtain
        new tokens. The callback signature::

            async def refresh_callback(refresh_token: str) -> dict[str, Any]

        The callback must return a dict with at least ``access_token``
        and optionally ``refresh_token``, ``expires_in``, ``scope``.

        Args:
            user_id: Internal user ID.
            provider: OAuth provider name.
            refresh_callback: Async function to call for token refresh.
            buffer_seconds: Refresh if token expires within this many seconds.

        Returns:
            A valid OAuthTokenInfo, or None if no token exists.
        """
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
        """Find the most recent valid token for a provider (any user).

        Used at startup to pre-inject stored OAuth tokens into MCP servers
        before any specific user is known.
        """
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


    async def get_or_create_user(
        self,
        app_id: str | None,
        provider: str,
        external_id: str,
        **fields: Any,
    ) -> tuple[UserInfo, bool]:
        """Get existing user or create a new one.

        Returns (user, created) where created=True if a new user was made.
        """
        existing = await self.find_user(app_id, provider, external_id)
        if existing:
            return existing, False
        try:
            user = await self.create_user(app_id, provider, external_id, **fields)
            return user, True
        except ValueError:
            existing = await self.find_user(app_id, provider, external_id)
            if existing:
                return existing, False
            raise  # pragma: no cover
