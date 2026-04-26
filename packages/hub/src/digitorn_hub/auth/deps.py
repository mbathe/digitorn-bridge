from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import ApiToken, User
from .jwt import JWTError, decode
from .tokens import (
    has_scope,
    hash_for_lookup,
    looks_like_api_token,
    parse_authorization_header,
)


class AuthPrincipal:
    def __init__(self, user: User, scopes: list[str], via_token: bool):
        self.user = user
        self.scopes = scopes
        self.via_token = via_token

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    def has(self, scope: str) -> bool:
        return has_scope(self.scopes, scope)


async def _resolve_jwt(
    raw: str, session: AsyncSession
) -> AuthPrincipal:
    try:
        payload = decode(raw, expected_type="access")
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    user_id = uuid.UUID(payload["sub"])
    user = await session.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found or disabled")
    return AuthPrincipal(user=user, scopes=["*"], via_token=False)


async def _resolve_api_token(
    raw: str, session: AsyncSession
) -> AuthPrincipal:
    token_hash = hash_for_lookup(raw)
    stmt = (
        select(ApiToken, User)
        .join(User, User.id == ApiToken.user_id)
        .where(ApiToken.token_hash == token_hash)
    )
    row = (await session.execute(stmt)).first()
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    api_token, user = row
    if api_token.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token revoked")
    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user disabled")

    await session.execute(
        update(ApiToken)
        .where(ApiToken.id == api_token.id)
        .values(last_used_at=datetime.now(timezone.utc))
    )
    await session.commit()

    return AuthPrincipal(user=user, scopes=list(api_token.scopes), via_token=True)


async def get_current_principal(
    authorization: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_session),
) -> AuthPrincipal:
    raw = parse_authorization_header(authorization)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    if looks_like_api_token(raw):
        return await _resolve_api_token(raw, session)
    return await _resolve_jwt(raw, session)


async def get_current_user(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> User:
    return principal.user


def require_scope(scope: str):
    async def _checker(
        principal: AuthPrincipal = Depends(get_current_principal),
    ) -> AuthPrincipal:
        if not principal.has(scope):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"missing required scope: {scope}"
            )
        return principal

    return _checker


def require_admin(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> AuthPrincipal:
    if principal.user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return principal
